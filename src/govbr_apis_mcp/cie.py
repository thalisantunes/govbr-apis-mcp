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
from datetime import date, datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

API_ID = "meiaentrada-cie-validador"
VALIDATOR_PATH = "/public/validador"
# Every date the issuers (and our callers) use: YYYY-MM-DD optionally followed by a time,
# YYYYMMDD and DD/MM/YYYY. Anything else is not a date, so it can never be read as one.
DATE_FORMATS = (
    (re.compile(r"(\d{4}-\d{2}-\d{2})(?:[T ]\d.*)?", re.ASCII), "%Y-%m-%d"),
    (re.compile(r"(\d{8})", re.ASCII), "%Y%m%d"),
    (re.compile(r"(\d{2}/\d{2}/\d{4})", re.ASCII), "%d/%m/%Y"),
)

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
    # False when no validator gave an answer (network or HTTP failure, truncated or
    # unreadable response). That is not a refusal: nothing is known about the card, so
    # the caller retries or falls back to documents — it never denies on this alone.
    validated: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "validated": self.validated,
            "reason": self.reason,
            "provider": self.provider,
            "holder": self.holder,
            "raw": self.raw,
        }


def parse_date(value: Any) -> date | None:
    """Read a date in any format the issuers use; None if it is not a real calendar date.

    Accepts YYYY-MM-DD (optionally followed by T or a space and a time), YYYYMMDD and
    DD/MM/YYYY. Dates must be compared as dates, never as text: as strings "20260930"
    sorts after "2026-12-20", so an expired card used to pass.
    """
    if not isinstance(value, str):
        return None
    for pattern, fmt in DATE_FORMATS:
        match = pattern.fullmatch(value.strip())
        if match:
            try:
                return datetime.strptime(match.group(1), fmt).date()
            except ValueError:  # right shape, impossible date (month 13, 31 April...)
                return None
    return None


def _require_date(value: str, field_name: str) -> date:
    parsed = parse_date(value)
    if parsed is None:
        raise InvalidInput(f"{field_name}: use YYYYMMDD, YYYY-MM-DD ou DD/MM/YYYY — got {value!r}")
    if not 1900 <= parsed.year <= 2100:
        raise InvalidInput(f"{field_name}: {value!r} does not look like a date")
    return parsed


def normalize_date(value: str, field_name: str) -> str:
    """Accept YYYYMMDD, YYYY-MM-DD or DD/MM/YYYY; always send YYYYMMDD."""
    return _require_date(value, field_name).strftime("%Y%m%d")


def today_br(now: datetime | None = None) -> date:
    """Today's date in Brazil (America/Sao_Paulo), never UTC's.

    From 21:00 in Brasília the UTC date is already tomorrow: it would refuse a card on the
    last day of its validity and send tomorrow as the event date. `now` (timezone-aware)
    exists so tests can pin the clock.
    """
    zone = ZoneInfo("America/Sao_Paulo")
    return (now or datetime.now(zone)).astimezone(zone).date()


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
        else today_br().strftime("%Y%m%d"),
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
        problem = _expiry_problem(
            parse_date(payload.get("dataExpiracao")), expiry_reference(data_evento)
        )
        if problem:
            return CieDecision(False, problem, {}, payload, "dne")
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
    unvalidated = _unvalidated(response, "dne")
    if unvalidated:
        return unvalidated
    decision = interpret(response["body"], data_evento)
    decision.provider = "dne"
    return decision


def _unvalidated(response: dict[str, Any], provider: str) -> CieDecision | None:
    """A "not validated" decision when the gateway response cannot support any verdict.

    The verdict is only read from a complete 2xx JSON object: an error page, a cut body
    or an unparseable one says nothing about the card. `raw` keeps just the diagnosis,
    because the text of a truncated body can hold personal data.
    """
    body = response.get("body")
    status = response.get("status")
    raw = {k: response[k] for k in ("error", "status") if k in response}
    if isinstance(body, dict):
        raw.update({k: body[k] for k in ("truncated", "parse_error") if k in body})

    if "error" in response:
        reason = f"falha na chamada: {response['error']}"
    elif not (isinstance(status, int) and 200 <= status < 300):
        reason = f"validador não respondeu com HTTP 2xx (status: {status}), sem decisão"
    elif isinstance(body, dict) and body.get("truncated"):
        reason = "resposta truncada/ilegível do validador, sem decisão (acima do limite do gateway)"
    elif isinstance(body, dict) and body.get("parse_error"):
        reason = "resposta truncada/ilegível do validador, sem decisão (JSON inválido)"
    elif not isinstance(body, dict):
        reason = f"resposta inesperada do validador (corpo {type(body).__name__}), sem decisão"
    else:
        return None
    return CieDecision(False, reason, {}, raw, provider, validated=False)


# --- FESN --------------------------------------------------------------------
# Second issuer, found when a legitimate IFRS card failed against DNE. Different
# contract entirely: GET by use code, no auth, no birth date, no status envelope —
# a 200 with a body means the card exists. Confirms that "not in DNE" can never be
# treated as "not entitled".
FESN_API_ID = "fesn-cie-validador"
FESN_PATH = "/api/students/usecode"
# The use code is embedded in a URL path, so it must be a plain token: "/", "?", "#",
# spaces or dot-only segments would change the resource FESN is asked about.
USE_CODE_RE = re.compile(r"(?!\.+$)[A-Za-z0-9._-]{1,64}")


def expiry_reference(data_evento: str | None) -> date:
    """The date a card's validity must be checked against: the event, not the purchase.

    Checking against today lets a card expiring in September approve a ticket for a
    December show — buying early, not sophisticated fraud.
    """
    if data_evento:
        return _require_date(data_evento, "dataEvento")
    return today_br()


def _expiry_problem(expires: date | None, event_day: date) -> str | None:
    """Why a card cannot be accepted for `event_day`, or None when its validity covers it.

    Fails closed: with no readable expiry there is no way to know the card is still valid.
    """
    if expires is None:
        return "validade não informada/ilegível — sem como confirmar que cobre a data do evento"
    if expires < event_day:
        return f"carteirinha vence em {expires.isoformat()}, antes da data do evento"
    return None


def interpret_fesn(
    payload: dict[str, Any],
    data_nascimento: str | None = None,
    data_evento: str | None = None,
    *,
    codigo_uso: str,
) -> CieDecision:
    """Turn an FESN response into a decision.

    Checks this API does not do for us:
    - record: the payload must carry the use code we asked for. An error object, an
      empty body or someone else's record is not the card, whatever else it contains;
    - expiry: `validity` is returned but never enforced — it must be readable and is
      compared against the event date, not today;
    - identity: the card validates from the use code alone, so when a birth date is
      supplied we compare it ourselves (and refuse if FESN gave none to compare).

    Be honest about what that birth-date check is worth: FESN answers unauthenticated
    and returns `birth` in the body, so anyone holding the use code can read the date
    and type it back. It catches typos and casual misuse, NOT a motivated attacker.
    Real identity binding has to happen where possession meets the person — a ticket
    named after the CIE holder, checked at the gate.
    """
    returned = str(payload.get("useCode") or "").strip().casefold()
    if not returned or returned != codigo_uso.strip().casefold():
        return CieDecision(
            False,
            "carteirinha não encontrada na base FESN — a resposta não traz o código consultado",
            {},
            payload,
            "fesn",
        )

    expires = parse_date(payload.get("validity"))
    problem = _expiry_problem(expires, expiry_reference(data_evento))
    if problem:
        return CieDecision(False, problem, {}, payload, "fesn")

    if data_nascimento:
        expected = _require_date(data_nascimento, "dataNascimento")
        birth = parse_date(payload.get("birth"))
        if birth is None:
            return CieDecision(
                False,
                "data de nascimento da FESN não informada/ilegível — "
                "não dá para confirmar a titularidade",
                {},
                payload,
                "fesn",
            )
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
        "dataExpiracao": expires.isoformat(),
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
    code = (codigo_uso or "").strip()
    if not code:
        raise InvalidInput("codigoUso é obrigatório")
    if not USE_CODE_RE.fullmatch(code):
        raise InvalidInput(
            "codigoUso: use 1 a 64 letras, dígitos, '.', '_' ou '-' "
            f"(sem '/', '?', '#' ou espaços) — got {code!r}"
        )
    if caller is None:
        from .server import call_api as caller

    response = caller(FESN_API_ID, f"{FESN_PATH}/{quote(code, safe='')}", method="GET")
    if response.get("status") == 404:
        # FESN answers an unknown use code with a 404 and an error object ({"name":
        # "NotFoundError"...}). That is its verdict on the card, unlike a 5xx; reading the
        # error object as a student record once approved a card that does not exist.
        return CieDecision(
            False, "carteirinha não encontrada na base FESN", {}, {"status": 404}, "fesn"
        )
    unvalidated = _unvalidated(response, "fesn")
    if unvalidated:
        return unvalidated
    return interpret_fesn(response["body"], data_nascimento, data_evento, codigo_uso=code)


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
    dne.validated = dne.validated and fesn.validated
    return dne
