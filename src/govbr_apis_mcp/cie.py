"""CIE (student card) eligibility check for half-price tickets.

Thin business layer over the catalog entries `meiaentrada-cie-validador` (DNE) and
`fesn-cie-validador` (FESN): it turns each validator's raw payload into a decision a box
office can act on, and keeps the quirks of those APIs in one place.

Quirks worth knowing (verified 2026-08-07 against the live DNE endpoint):
- Always HTTP 200, even on failure; the verdict is in `status` (bool or the *string*
  "true") and `codigoRetorno`.
- `dataNascimento` must be YYYYMMDD — DDMMYYYY makes the server return HTTP 500.
- codigoRetorno 5 means the request shape is wrong; 2 means the card does not exist.
  Confusing one for the other sends you debugging the wrong thing.
- Since it answers 200 even when it fails, only the codes in RETURN_CODES are verdicts: 1
  approves (with a true `status`), 2 is the one known refusal, 5 is our own request out of
  contract. Any other code is an answer we do not recognize, so it is "not validated".

Two rules hold for every issuer:
- Fail closed: whatever a decision depends on (HTTP status, use code, expiry, birth date)
  must be present and readable, otherwise the card is not approved.
- "Not validated" is not "refused": when there is no usable answer to decide on (network
  or HTTP failure, truncated or unreadable body, a payload without a verdict or with an
  unrecognized or contradictory one, or one base saying "not found" while another failed)
  the decision has validated=False and the caller retries or asks for documents; it never
  denies on that alone.
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
# Every date the issuers (and our callers) use: YYYY-MM-DD optionally followed by a real
# time of day (HH:MM[:SS[.fff]] with an optional Z or UTC offset, which is not applied: the
# date is kept as written), YYYYMMDD and DD/MM/YYYY. Anything else is not a date, so it can
# never be read as one.
_TIME = (
    r"(?:[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:[.,]\d+)?)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)?)?"
)
DATE_FORMATS = (
    (re.compile(r"(\d{4}-\d{2}-\d{2})" + _TIME, re.ASCII), "%Y-%m-%d"),
    (re.compile(r"(\d{8})", re.ASCII), "%Y%m%d"),
    (re.compile(r"(\d{2}/\d{2}/\d{4})", re.ASCII), "%d/%m/%Y"),
)

# DNE return codes the layer understands, observed on the live API. The API answers HTTP 200
# even when it fails, so only these are verdicts: any other code is an answer we do not
# recognize, not a refusal. Code 2 is ambiguous by design: the API answers "documento
# inexistente" both for an unknown codigoUso and for a real card whose dataNascimento does
# not match (verified 2026-08-07 with a known-good card and a wrong birth date).
RETURN_CODES = {
    "1": "documento válido",
    "2": "documento não encontrado — confira o código de uso E a data de nascimento; "
    "a API responde o mesmo para código inexistente e data que não bate",
    "5": "parâmetros inválidos (payload fora do contrato) — erro do nosso pedido, "
    "sem decisão sobre a carteirinha",
}
APPROVAL_CODE = "1"
REFUSAL_CODES = ("2",)  # the one refusal the API is known to give for a card


# Keys of a validator payload that carry no personal data. A refusal keeps only these in
# `raw`: the rest (name, CPF, birth date, photo...) belongs to whoever holds the card, and
# a caller who merely guessed a code or a birth date must not receive it.
SAFE_RAW_KEYS = ("status", "codigoRetorno", "mensagem", "message", "dataExpiracao", "validity")
MAX_TEXT = 200  # an issuer's free text is clipped to this many characters wherever we copy it


class InvalidInput(ValueError):
    """Raised before any network call, so bad input never becomes a confusing 500."""


@dataclass
class CieDecision:
    eligible: bool
    reason: str
    holder: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    # False when there is no usable answer to decide on: a network, gateway or HTTP failure,
    # a truncated or unreadable response, a payload without a verdict, or (across bases) one
    # base saying "not found" while another failed. That is not a refusal: nothing is known
    # about the card, so the caller retries or falls back to documents — it never denies on
    # this alone.
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


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_raw(payload: dict[str, Any]) -> dict[str, Any]:
    """The part of a validator payload that carries no personal data (see SAFE_RAW_KEYS).

    Only scalar values survive and free text is clipped: an issuer's text can say anything.
    """
    safe: dict[str, Any] = {}
    for key in SAFE_RAW_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            safe[key] = _clip(value)
        elif value is None or isinstance(value, (bool, int, float)):
            safe[key] = value
    return safe


def _refusal(reason: str, payload: dict[str, Any], provider: str | None) -> CieDecision:
    """A refusal: a validator answered and the card does not qualify. No personal data in raw."""
    return CieDecision(False, reason, {}, _safe_raw(payload), provider)


def _unvalidated(reason: str, payload: dict[str, Any], provider: str | None) -> CieDecision:
    """No usable answer to decide on (see CieDecision.validated). No personal data in raw."""
    return CieDecision(False, reason, {}, _safe_raw(payload), provider, validated=False)


def parse_date(value: Any) -> date | None:
    """Read a date in any format the issuers use; None if it is not a real calendar date.

    Accepts YYYY-MM-DD (optionally followed by T or a space and a time), YYYYMMDD and
    DD/MM/YYYY. Dates must be compared as dates, never as text: as strings "20260930"
    sorts after "2026-12-20".
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
        if data_evento is not None
        else today_br().strftime("%Y%m%d"),
        "cnpjPromotor": cnpj,
    }


def interpret(payload: dict[str, Any], data_evento: str | None = None) -> CieDecision:
    """Turn a DNE response into an eligibility decision.

    `dataExpiracao` is enforced here against the event date: whether the DNE API
    already accounts for the `dataEvento` we send is undocumented and unverified,
    so we never rely on it alone.

    Only a verdict on the card is a refusal. By `codigoRetorno` (see RETURN_CODES):
    - 1 with a true `status` approves; without it the answer contradicts itself;
    - 2 is the one refusal the API is known to give, and with no code an explicit false
      `status` is one too;
    - 5 is our own request out of contract and any other code is one we do not know: neither
      says anything about the card;
    - a true `status` with a code other than 1 contradicts itself, and a payload with neither
      a boolean `status` nor a code says nothing.
    Whatever is not a verdict on the card comes back "not validated", not "not eligible".
    """
    status = payload.get("status")
    approved = status is True or (isinstance(status, str) and status.lower() == "true")
    denied = status is False or (isinstance(status, str) and status.lower() == "false")
    raw_code = payload.get("codigoRetorno")
    code = None if raw_code in (None, "") else str(raw_code)
    text = payload.get("mensagem")
    message = _clip(text.strip()) if isinstance(text, str) else ""

    if approved:
        if code not in (None, APPROVAL_CODE):
            return _unvalidated(
                f"veredito contraditório: status verdadeiro com codigoRetorno {_clip(code, 40)}, "
                "sem decisão",
                payload,
                "dne",
            )
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
            return _refusal(problem, payload, "dne")
        return CieDecision(True, message or "CIE válida", holder, payload, "dne")

    if code is None:
        if denied:
            return _refusal(
                message or "recusada pelo validador (status falso, sem codigoRetorno)",
                payload,
                "dne",
            )
        return _unvalidated(
            "resposta do validador sem veredito (sem status booleano nem codigoRetorno), "
            "sem decisão",
            payload,
            "dne",
        )

    known = RETURN_CODES.get(code)
    extra = f" ({message})" if message and (known is None or message not in known) else ""
    if code in REFUSAL_CODES:
        return _refusal(f"{known}{extra}", payload, "dne")
    if code == APPROVAL_CODE:
        return _unvalidated(
            f"codigoRetorno {code} ({known}) sem status verdadeiro: veredito contraditório, "
            "sem decisão",
            payload,
            "dne",
        )
    if known:
        return _unvalidated(f"{known}{extra}", payload, "dne")
    return _unvalidated(
        f"codigoRetorno {_clip(code, 40)} não reconhecido pelo validador{extra}, sem decisão",
        payload,
        "dne",
    )


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
    can inject a stub without touching the network. The clock is read here, once, before
    the request: the day sent to the DNE is the day the card is judged against.
    """
    event = expiry_reference(data_evento).isoformat()
    body = build_payload(codigo_uso, data_nascimento, nome_evento, cnpj_promotor, event)
    if caller is None:
        from .server import call_api as caller  # imported lazily: avoids a cycle

    response = caller(API_ID, VALIDATOR_PATH, method="POST", body=body)
    unusable = _unusable_response(response, "dne")
    if unusable:
        return unusable
    return interpret(response["body"], event)


def _unusable_response(response: dict[str, Any], provider: str) -> CieDecision | None:
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
# contract entirely: GET by use code, no auth, no birth date, no status envelope. The
# verdict is the HTTP status plus a record whose useCode is the one we asked for; a 200
# with any other body proves nothing about the card. Confirms that "not in DNE" can
# never be treated as "not entitled".
FESN_API_ID = "fesn-cie-validador"
FESN_PATH = "/api/students/usecode"
# The use code is embedded in a URL path, so it must be a plain token: "/", "?", "#",
# spaces or dot-only segments would change the resource FESN is asked about. That is a FESN
# precondition only: the DNE takes the code in a JSON body and accepts any non-empty text.
USE_CODE_RE = re.compile(r"(?!\.+$)[A-Za-z0-9._-]{1,64}")


def _validate_fesn_input(
    codigo_uso: str, data_nascimento: str | None, data_evento: str | None
) -> tuple[str, str]:
    """Check what check_fesn takes from the caller, and read the clock, before any network call.

    Returns the cleaned use code and the event day (ISO). A birth date or an event date that
    is supplied but empty is an error, not a skipped check; None is how a caller says "none".
    """
    code = (codigo_uso or "").strip()
    if not code:
        raise InvalidInput("codigoUso é obrigatório")
    if not USE_CODE_RE.fullmatch(code):
        raise InvalidInput(
            "codigoUso: use 1 a 64 letras, dígitos, '.', '_' ou '-' "
            f"(sem '/', '?', '#' ou espaços) — got {code!r}"
        )
    if data_nascimento is not None:
        _require_date(data_nascimento, "dataNascimento")
    return code, expiry_reference(data_evento).isoformat()


def expiry_reference(data_evento: str | None) -> date:
    """The date a card's validity must be checked against: the event, not the purchase.

    Checking against today lets a card expiring in September approve a ticket for a
    December show — buying early, not sophisticated fraud.
    """
    if data_evento is not None:
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
      empty body or someone else's record says nothing about the card (not validated);
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
        return _unvalidated(
            "resposta da FESN não traz o código de uso consultado (ausente ou diferente), "
            "sem decisão",
            payload,
            "fesn",
        )

    expires = parse_date(payload.get("validity"))
    problem = _expiry_problem(expires, expiry_reference(data_evento))
    if problem:
        return _refusal(problem, payload, "fesn")

    if data_nascimento is not None:
        expected = _require_date(data_nascimento, "dataNascimento")
        birth = parse_date(payload.get("birth"))
        if birth is None:
            return _refusal(
                "data de nascimento da FESN não informada/ilegível — "
                "não dá para confirmar a titularidade",
                payload,
                "fesn",
            )
        if birth != expected:
            return _refusal(
                "data de nascimento não confere com a carteirinha — confirme a titularidade",
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
    data_evento: str | None = None,
    caller: Any = None,
) -> CieDecision:
    """Validate a CIE against the FESN base.

    Raises InvalidInput, before any network call, for a malformed use code or date. The
    clock is read before the request too, so a failure there never costs a request.
    """
    code, event = _validate_fesn_input(codigo_uso, data_nascimento, data_evento)
    if caller is None:
        from .server import call_api as caller

    response = caller(FESN_API_ID, f"{FESN_PATH}/{quote(code, safe='')}", method="GET")
    body = response.get("body")
    if (
        response.get("status") == 404
        and isinstance(body, dict)
        and body.get("name") == "NotFoundError"
    ):
        # An unknown use code is expected to come back as a 404 carrying this error object:
        # FESN's verdict on the card. A 404 with any other body (a proxy or WAF page, plain
        # text) says nothing about the card and falls through to "not validated".
        return CieDecision(
            False, "carteirinha não encontrada na base FESN", {}, {"status": 404}, "fesn"
        )
    unusable = _unusable_response(response, "fesn")
    if unusable:
        return unusable
    return interpret_fesn(body, data_nascimento, event, codigo_uso=code)


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
    decision returned is the first approval. When every base fails, no base owns the
    answer: `provider` is None, the reasons of all of them are joined, and the result is
    "not validated" unless every base actually answered.

    What every base needs (use code, dates, CNPJ) is validated before the first network
    call, and the clock is read once, so the same day is used for both bases. A use code
    that is not a FESN path token is not an error: the FESN leg is skipped, saying so, and
    the DNE verdict stands.
    """
    event = expiry_reference(data_evento).isoformat()
    dne = check(codigo_uso, data_nascimento, nome_evento, cnpj_promotor, event, caller)
    if dne.eligible:
        return dne

    code = codigo_uso.strip()  # not empty: check() would have raised
    if USE_CODE_RE.fullmatch(code):
        fesn = check_fesn(code, data_nascimento, event, caller)
        if fesn.eligible:
            return fesn
        fesn_reason, fesn_raw, fesn_answered = fesn.reason, fesn.raw, fesn.validated
    else:
        fesn_reason = (
            "não consultada: o código não é um useCode FESN "
            "(1 a 64 letras, dígitos, '.', '_' ou '-')"
        )
        fesn_raw, fesn_answered = {}, True  # a base that does not apply leaves nothing missing

    validated = dne.validated and fesn_answered
    if validated:
        head = "as bases consultadas responderam que a carteirinha não vale"
        tail = "Existem emissoras sem API — trate como pendência documental, não como recusa."
    else:
        head = "sem decisão: faltou resposta utilizável de pelo menos uma base consultada"
        tail = "Tente de novo ou trate como pendência documental, não como recusa."
    return CieDecision(
        False,
        f"{head} (DNE: {dne.reason}; FESN: {fesn_reason}). {tail}",
        {},
        {"dne": dne.raw, "fesn": fesn_raw},
        None,
        validated=validated,
    )
