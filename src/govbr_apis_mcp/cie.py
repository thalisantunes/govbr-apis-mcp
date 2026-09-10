"""CIE (student card) eligibility check for half-price tickets.

Thin business layer over the catalog entry `meiaentrada-cie-validador`: it turns the
validator's raw payload into a decision a box office can act on, and keeps the quirks
of that API in one place.

Quirks worth knowing (verified 2026-08-07 against the live endpoint):
- Always HTTP 200, even on failure; the verdict is in `status` (bool or the *string*
  "true") and `codigoRetorno`.
- `dataNascimento` must be YYYYMMDD — DDMMYYYY makes the server return HTTP 500.
- codigoRetorno 5 means the request shape is wrong; 2 means the card does not exist.
  Confusing one for the other sends you debugging the wrong thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

API_ID = "meiaentrada-cie-validador"
VALIDATOR_PATH = "/public/validador"
DATE_RE = re.compile(r"^\d{8}$")

# Observed on the live API. Anything unmapped falls back to the API's own `mensagem`.
# Code 2 is ambiguous by design: the API answers "documento inexistente" both for an
# unknown codigoUso and for a real card whose dataNascimento does not match (verified
# 2026-08-07 with a known-good card and a wrong birth date).
RETURN_CODES = {
    "1": "documento válido",
    "2": "documento não encontrado — confira o código de uso E a data de nascimento; "
    "a API responde o mesmo para código inexistente e data que não bate",
    "5": "parâmetros inválidos (payload fora do contrato)",
}


class InvalidInput(ValueError):
    """Raised before any network call, so bad input never becomes a confusing 500."""


@dataclass
class CieDecision:
    eligible: bool
    reason: str
    holder: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "provider": self.provider,
            "holder": self.holder,
            "raw": self.raw,
        }


def normalize_date(value: str, field_name: str) -> str:
    """Accept YYYYMMDD, YYYY-MM-DD or DD/MM/YYYY; always send YYYYMMDD."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 8:
        raise InvalidInput(f"{field_name}: use YYYYMMDD, YYYY-MM-DD ou DD/MM/YYYY — got {value!r}")
    if "/" in (value or "") and not (value or "").startswith(digits[:4]):
        digits = digits[4:] + digits[2:4] + digits[:2]  # DD/MM/YYYY -> YYYYMMDD
    if not DATE_RE.match(digits) or not 1900 <= int(digits[:4]) <= 2100:
        raise InvalidInput(f"{field_name}: {value!r} does not look like a date")
    return digits


def build_payload(
    codigo_uso: str,
    data_nascimento: str,
    nome_evento: str,
    cnpj_promotor: str,
    data_evento: str | None = None,
) -> dict[str, str]:
    if not (codigo_uso or "").strip():
        raise InvalidInput("codigoUso é obrigatório (é o código de uso impresso/QR da CIE)")
    cnpj = re.sub(r"\D", "", cnpj_promotor or "")
    if len(cnpj) != 14:
        raise InvalidInput(f"cnpjPromotor deve ter 14 dígitos — got {cnpj_promotor!r}")
    return {
        "codigoUso": codigo_uso.strip(),
        "dataNascimento": normalize_date(data_nascimento, "dataNascimento"),
        "nomeEvento": (nome_evento or "").strip() or "evento",
        "dataEvento": normalize_date(data_evento, "dataEvento")
        if data_evento
        else datetime.now(tz=UTC).strftime("%Y%m%d"),
        "cnpjPromotor": cnpj,
    }


def interpret(payload: dict[str, Any], data_evento: str | None = None) -> CieDecision:
    """Turn a validator response into an eligibility decision.

    `dataExpiracao` is enforced here against the event date: whether the DNE API
    already accounts for the `dataEvento` we send is undocumented and unverified,
    so we never rely on it alone.
    """
    status = payload.get("status")
    approved = status is True or (isinstance(status, str) and status.lower() == "true")
    code = str(payload.get("codigoRetorno", "")) or None
    message = (payload.get("mensagem") or "").strip()

    if approved:
        holder = {
            key: payload.get(key)
            for key in (
                "nome",
                "nomeSocial",
                "cpf",
                "documento",
                "instituicao",
                "curso",
                "nivelEscolaridade",
                "entidade",
                "municipio",
                "certificado",
                "dataExpiracao",
                "urlFoto",
            )
            if payload.get(key)
        }
        holder["has_photo"] = bool(payload.get("foto") or payload.get("urlFoto"))
        expires = (payload.get("dataExpiracao") or "")[:10]
        if expires and expires < expiry_reference(data_evento):
            return CieDecision(
                False,
                f"carteirinha vence em {expires}, antes da data do evento",
                {},
                payload,
                "dne",
            )
        return CieDecision(True, message or "CIE válida", holder, payload)

    reason = RETURN_CODES.get(code or "", message or "CIE não validada")
    if code and code in RETURN_CODES and message and message not in reason:
        reason = f"{reason} ({message})"
    return CieDecision(False, reason, {}, payload)


def check(
    codigo_uso: str,
    data_nascimento: str,
    nome_evento: str,
    cnpj_promotor: str,
    data_evento: str | None = None,
    caller: Any = None,
) -> CieDecision:
    """Validate a CIE against the DNE base (UNE/UBES/ANPG) through the MCP gateway.

    `caller` defaults to the server's call_api, and exists so tests and the demo mode
    can inject a stub without touching the network.
    """
    body = build_payload(codigo_uso, data_nascimento, nome_evento, cnpj_promotor, data_evento)
    if caller is None:
        from .server import call_api as caller  # imported lazily: avoids a cycle

    response = caller(API_ID, VALIDATOR_PATH, method="POST", body=body)
    if "error" in response:
        return CieDecision(False, f"falha na chamada: {response['error']}", {}, response, "dne")

    payload = response.get("body")
    if not isinstance(payload, dict):
        return CieDecision(
            False, f"resposta inesperada do validador: {payload!r}", {}, response, "dne"
        )
    decision = interpret(payload, data_evento)
    decision.provider = "dne"
    return decision


# --- FESN --------------------------------------------------------------------
# Second issuer, found when a legitimate IFRS card failed against DNE. Different
# contract entirely: GET by use code, no auth, no birth date, no status envelope —
# a 200 with a body means the card exists. Confirms that "not in DNE" can never be
# treated as "not entitled".
FESN_API_ID = "fesn-cie-validador"
FESN_PATH = "/api/students/usecode"


def expiry_reference(data_evento: str | None) -> str:
    """The date a card's validity must be checked against: the event, not the purchase.

    Checking against today lets a card expiring in September approve a ticket for a
    December show — buying early, not sophisticated fraud.
    """
    if data_evento:
        digits = normalize_date(data_evento, "dataEvento")
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


def interpret_fesn(
    payload: dict[str, Any],
    data_nascimento: str | None = None,
    data_evento: str | None = None,
) -> CieDecision:
    """Turn an FESN response into a decision.

    Two checks this API does not do for us:
    - expiry: `validity` is returned but never enforced — and it must be compared
      against the event date, not today;
    - identity: the card validates from the use code alone, so when a birth date is
      supplied we compare it ourselves.

    Be honest about what that birth-date check is worth: FESN answers unauthenticated
    and returns `birth` in the body, so anyone holding the use code can read the date
    and type it back. It catches typos and casual misuse, NOT a motivated attacker.
    Real identity binding has to happen where possession meets the person — a ticket
    named after the CIE holder, checked at the gate.
    """
    if not payload.get("useCode") and not payload.get("name"):
        return CieDecision(False, "carteirinha não encontrada na base FESN", {}, payload, "fesn")

    validity = (payload.get("validity") or "")[:10]
    if validity and validity < expiry_reference(data_evento):
        return CieDecision(
            False,
            f"carteirinha vence em {validity}, antes da data do evento",
            {},
            payload,
            "fesn",
        )

    birth = (payload.get("birth") or "")[:10].replace("-", "")
    if data_nascimento and birth:
        expected = normalize_date(data_nascimento, "dataNascimento")
        if birth != expected:
            return CieDecision(
                False,
                "data de nascimento não confere com a carteirinha — confirme a titularidade",
                {},
                payload,
                "fesn",
            )

    holder = {
        "nome": payload.get("name"),
        "cpf": payload.get("cpf"),
        "documento": payload.get("rg"),
        "instituicao": payload.get("entity"),
        "curso": payload.get("course"),
        "nivelEscolaridade": payload.get("courseType"),
        "entidade": "FESN",
        "dataExpiracao": validity or None,
        "urlFoto": payload.get("photo"),
    }
    holder = {k: v for k, v in holder.items() if v}
    holder["has_photo"] = bool(payload.get("photo"))
    return CieDecision(True, "CIE válida (base FESN)", holder, payload, "fesn")


def check_fesn(
    codigo_uso: str,
    data_nascimento: str | None = None,
    caller: Any = None,
    data_evento: str | None = None,
) -> CieDecision:
    """Validate a CIE against the FESN base."""
    if not (codigo_uso or "").strip():
        raise InvalidInput("codigoUso é obrigatório")
    if caller is None:
        from .server import call_api as caller

    response = caller(FESN_API_ID, f"{FESN_PATH}/{codigo_uso.strip()}", method="GET")
    if "error" in response:
        return CieDecision(False, f"falha na chamada: {response['error']}", {}, response, "fesn")
    payload = response.get("body")
    if not isinstance(payload, dict):
        return CieDecision(
            False, f"resposta inesperada do validador: {payload!r}", {}, response, "fesn"
        )
    return interpret_fesn(payload, data_nascimento, data_evento)


def check_any(
    codigo_uso: str,
    data_nascimento: str,
    nome_evento: str,
    cnpj_promotor: str,
    data_evento: str | None = None,
    caller: Any = None,
) -> CieDecision:
    """Try every known issuer before declaring a card invalid.

    A student whose card comes from a state entity or DCE is still entitled by law
    (Lei 12.933/2013); failing on the first base would deny a legitimate right. The
    decision returned is the first approval, or the DNE failure with a note that the
    other bases were checked too.
    """
    dne = check(codigo_uso, data_nascimento, nome_evento, cnpj_promotor, data_evento, caller)
    if dne.eligible:
        return dne

    fesn = check_fesn(codigo_uso, data_nascimento, caller, data_evento)
    if fesn.eligible:
        return fesn

    dne.reason = (
        f"não validou em nenhuma base consultada (DNE: {dne.reason}; FESN: {fesn.reason}). "
        "Existem emissoras sem API — trate como pendência documental, não como recusa."
    )
    return dne
