import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from govbr_apis_mcp import cie
from govbr_apis_mcp.cie import (
    InvalidInput,
    build_payload,
    check,
    check_any,
    check_fesn,
    interpret,
    interpret_fesn,
    normalize_date,
    parse_date,
    today_br,
)

# Before the 2026-09-30 expiries below: those cards are valid today but expire before the
# 2026-12-20 event, which is what tells "checked against the event" from "against today".
TODAY = date(2026, 8, 7)
CODE = "AB12CD34"


def reply(body, status=200):
    """A response shaped like server.call_api's: HTTP status plus the parsed body."""
    return {"status": status, "url": "https://example.invalid/x", "body": body}


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    """No test may depend on the real clock: pin "today" for every test in this module."""
    monkeypatch.setattr(cie, "today_br", lambda: TODAY)


@pytest.mark.parametrize(
    "value,expected",
    [("20000115", "20000115"), ("2000-01-15", "20000115"), ("15/01/2000", "20000115")],
)
def test_normalize_date_accepts_common_formats(value, expected):
    assert normalize_date(value, "dataNascimento") == expected


@pytest.mark.parametrize(
    "value", ["15012000x", "2000", "", "não é data", "2000/01/15", "15.01.2000", None]
)
def test_normalize_date_rejects_garbage(value):
    with pytest.raises(InvalidInput):
        normalize_date(value, "dataNascimento")


def test_payload_matches_api_contract():
    """The API 500s on a non-YYYYMMDD birth date, so normalization happens before the call."""
    body = build_payload("ABC123", "15/01/2000", "Show X", "00.000.000/0001-91", "2026-09-07")
    assert body == {
        "codigoUso": "ABC123",
        "dataNascimento": "20000115",
        "nomeEvento": "Show X",
        "dataEvento": "20260907",
        "cnpjPromotor": "00000000000191",
    }


def test_payload_rejects_bad_cnpj():
    with pytest.raises(InvalidInput, match="cnpjPromotor"):
        build_payload("ABC", "20000115", "x", "123")


def test_payload_rejects_empty_codigo_uso():
    with pytest.raises(InvalidInput, match="codigoUso"):
        build_payload("  ", "20000115", "x", "00000000000191")


def test_interpret_approved_extracts_holder():
    d = interpret(
        {
            "status": True,
            "mensagem": "documento válido",
            "nome": "MARIA",
            "cpf": "123",
            "instituicao": "UFMG",
            "urlFoto": "https://x/y.jpg",
            "curso": None,
            "dataExpiracao": "20270331",
        }
    )
    assert d.eligible
    assert d.holder["nome"] == "MARIA" and d.holder["has_photo"] is True
    assert "curso" not in d.holder  # campos nulos não poluem a conferência


def test_interpret_accepts_status_as_string():
    """The API returns the string "true" in some paths — the front-end handles both."""
    assert interpret({"status": "true", "nome": "X", "dataExpiracao": "20270331"}).eligible


def test_interpret_maps_known_return_codes():
    assert "parâmetros inválidos" in interpret({"status": False, "codigoRetorno": "5"}).reason


def test_code_2_reason_mentions_birth_date():
    """Code 2 also fires when the card exists but the birth date is wrong (verified live),
    so the message must send the operator to check both fields."""
    reason = interpret({"status": False, "codigoRetorno": "2"}).reason
    assert "data de nascimento" in reason and "código de uso" in reason


def test_interpret_falls_back_to_api_message():
    d = interpret({"status": False, "codigoRetorno": "99", "mensagem": "algo novo"})
    assert d.eligible is False and d.reason == "algo novo"


def test_check_surfaces_gateway_error():
    d = check("A", "20000115", "e", "00000000000191", caller=lambda *a, **k: {"error": "sem chave"})
    assert not d.eligible and "sem chave" in d.reason
    assert not d.validated and d.raw == {"error": "sem chave"}


def test_check_handles_non_dict_body():
    d = check("A", "20000115", "e", "00000000000191", caller=lambda *a, **k: reply("<html>"))
    assert not d.eligible and not d.validated and "inesperada" in d.reason


def test_check_sends_expected_request():
    captured = {}

    def fake_call(api_id, path, method=None, body=None, **kwargs):
        captured.update(api_id=api_id, path=path, method=method, body=body)
        return reply({"status": True, "nome": "MARIA", "dataExpiracao": "20270331"})

    d = check("ABC", "2000-01-15", "Show", "00000000000191", "2026-09-07", caller=fake_call)
    assert d.eligible
    assert captured["api_id"] == "meiaentrada-cie-validador"
    assert captured["path"] == "/public/validador"
    assert captured["method"] == "POST"
    assert captured["body"]["dataNascimento"] == "20000115"


FESN_OK = {
    "useCode": "AB12CD34",
    "name": "FULANA DE TAL",
    "birth": "1990-01-01T00:00:00",
    "cpf": "00000000000",
    "entity": "IFRS",
    "course": "ENGENHARIA",
    "courseType": "ED PROFISSIONAL",
    "validity": "2027-03-31 23:59:59",
    "photo": "https://example.invalid/p.jpg",
}


def test_fesn_valid_card():
    d = interpret_fesn(FESN_OK, "01/01/1990", codigo_uso=CODE)
    assert d.eligible and d.provider == "fesn"
    assert d.holder["instituicao"] == "IFRS" and d.holder["has_photo"] is True


def test_fesn_rejects_wrong_birth_date():
    """FESN validates from the use code alone, so we check identity ourselves."""
    d = interpret_fesn(FESN_OK, "1990-01-02", codigo_uso=CODE)
    assert not d.eligible and "não confere" in d.reason


def test_fesn_rejects_expired_card():
    """`validity` comes in the payload but the API never enforces it."""
    d = interpret_fesn({**FESN_OK, "validity": "2020-03-31 23:59:59"}, None, codigo_uso=CODE)
    assert not d.eligible and "vence em" in d.reason


def test_fesn_empty_body_has_no_verdict():
    d = interpret_fesn({}, None, codigo_uso=CODE)
    assert not d.eligible and not d.validated and "sem decisão" in d.reason


def test_check_any_falls_back_to_fesn(monkeypatch):
    """A legitimate card unknown to DNE must not be denied — the real AB12CD34 case."""

    def fake_call(api_id, path, **kwargs):
        if api_id == "meiaentrada-cie-validador":
            return reply({"status": False, "codigoRetorno": "2"})
        return reply(FESN_OK)

    d = check_any("AB12CD34", "1990-01-01", "Show", "00000000000191", caller=fake_call)
    assert d.eligible and d.provider == "fesn"


def test_check_any_propagates_event_date_to_fesn():
    def fake_call(api_id, path, **kwargs):
        if api_id == "meiaentrada-cie-validador":
            return reply({"status": False, "codigoRetorno": "2"})
        return reply({**FESN_OK, "validity": "2026-09-30 23:59:59"})

    d = check_any("AB12CD34", "1990-01-01", "Show", "00000000000191", "2026-12-20", fake_call)
    assert not d.eligible


# --- "today" is the Brazilian civil date, and injectable ------------------------


@pytest.mark.parametrize(
    "now,expected",
    [
        # 23:30 in Brasília is already tomorrow in UTC
        (datetime(2026, 12, 21, 2, 30, tzinfo=UTC), date(2026, 12, 20)),
        (datetime(2026, 12, 20, 23, 30, tzinfo=ZoneInfo("America/Sao_Paulo")), date(2026, 12, 20)),
        (datetime(2026, 12, 21, 2, 59, tzinfo=UTC), date(2026, 12, 20)),
        (datetime(2026, 12, 21, 3, 0, tzinfo=UTC), date(2026, 12, 21)),  # midnight in Brasília
    ],
)
def test_today_br_is_the_brazilian_date_not_utc(now, expected):
    assert today_br(now) == expected


def test_default_event_date_is_the_brazilian_today(monkeypatch):
    late_evening = datetime(2026, 12, 21, 2, 30, tzinfo=UTC)  # 23:30 in Brasília
    monkeypatch.setattr(cie, "today_br", lambda: today_br(late_evening))
    body = build_payload("ABC", "20000115", "x", "00000000000191")
    assert body["dataEvento"] == "20261220"  # UTC would have sent 20261221


def test_card_is_still_valid_on_its_last_day_late_in_the_evening(monkeypatch):
    """After 21:00 in Brasília the UTC date is already tomorrow and refused the card."""
    late_evening = datetime(2026, 12, 21, 2, 30, tzinfo=UTC)
    monkeypatch.setattr(cie, "today_br", lambda: today_br(late_evening))
    assert interpret_fesn(
        {**FESN_OK, "validity": "2026-12-20 23:59:59"}, None, codigo_uso=CODE
    ).eligible
    assert interpret({"status": True, "nome": "X", "dataExpiracao": "2026-12-20"}).eligible


# --- dates: one parser, a real calendar, compared as dates ------------------------


@pytest.mark.parametrize(
    "value", ["20261399", "2026-02-30", "31/04/2026", "2023-02-29", "00000000", "2026-00-10"]
)
def test_normalize_date_rejects_impossible_calendar_dates(value):
    with pytest.raises(InvalidInput):
        normalize_date(value, "dataEvento")


@pytest.mark.parametrize("value", ["2024-02-29", "29/02/2024", "20240229"])
def test_normalize_date_accepts_a_real_leap_day(value):
    assert normalize_date(value, "dataEvento") == "20240229"


EXPIRED = ["20260930", "2026-09-30", "30/09/2026", "2026-09-30T23:59:59", "2026-09-30 23:59:59"]
VALID_THROUGH_EVENT = [
    "20270331",
    "2027-03-31",
    "31/03/2027",
    "2027-03-31T23:59:59",
    "2027-03-31 23:59:59",
    "2027-03-31 23:59",
    "2027-03-31T23:59:59.123Z",
    "2027-03-31T23:59:59-03:00",
]


@pytest.mark.parametrize("expiry", EXPIRED)
def test_dne_expiry_is_compared_as_a_date_in_every_format(expiry):
    """Expiry is compared as a date, not as text ("20260930" > "2026-12-20" as strings), in
    every format; the DNE `status` alone is never trusted for it. This card is still valid
    today but expires before the event, so it also tells the event date from the purchase."""
    d = interpret({"status": True, "nome": "X", "dataExpiracao": expiry}, data_evento="2026-12-20")
    assert not d.eligible and "antes da data do evento" in d.reason and d.holder == {}


@pytest.mark.parametrize("expiry", VALID_THROUGH_EVENT)
def test_dne_card_valid_through_the_event_passes_in_every_format(expiry):
    d = interpret({"status": True, "nome": "X", "dataExpiracao": expiry}, data_evento="2026-12-20")
    assert d.eligible


@pytest.mark.parametrize("expiry", EXPIRED)
def test_fesn_expiry_is_compared_as_a_date_in_every_format(expiry):
    d = interpret_fesn({**FESN_OK, "validity": expiry}, None, "2026-12-20", codigo_uso=CODE)
    assert not d.eligible and "antes da data do evento" in d.reason


@pytest.mark.parametrize("expiry", VALID_THROUGH_EVENT)
def test_fesn_card_valid_through_the_event_passes_in_every_format(expiry):
    d = interpret_fesn({**FESN_OK, "validity": expiry}, None, "2026-12-20", codigo_uso=CODE)
    assert d.eligible and d.holder["dataExpiracao"] == "2027-03-31"


def test_card_is_valid_through_the_event_day_itself():
    payload = {"status": True, "nome": "X"}
    assert interpret({**payload, "dataExpiracao": "2026-12-20"}, data_evento="20261220").eligible
    assert not interpret(
        {**payload, "dataExpiracao": "2026-12-19"}, data_evento="20261220"
    ).eligible


UNREADABLE_DATES = [
    None,
    "",
    "   ",
    "amanhã",
    "2027-13-01",
    "31-03-2027",
    "2027-03-31x",
    "2027-02-30",
    "2027-03-31 9x",
    "2027-03-31T99:99",
    "2027-03-31 23:59:59 extra",
]


@pytest.mark.parametrize("expiry", UNREADABLE_DATES)
def test_dne_missing_or_unreadable_expiry_fails_closed(expiry):
    payload = {"status": True, "nome": "X", "cpf": "123"}
    if expiry is not None:
        payload["dataExpiracao"] = expiry
    d = interpret(payload, data_evento="2026-12-20")
    assert not d.eligible and "validade não informada/ilegível" in d.reason and d.holder == {}


@pytest.mark.parametrize("expiry", UNREADABLE_DATES)
def test_fesn_missing_or_unreadable_validity_fails_closed(expiry):
    payload = {k: v for k, v in FESN_OK.items() if k != "validity"}
    if expiry is not None:
        payload["validity"] = expiry
    d = interpret_fesn(payload, None, "2026-12-20", codigo_uso=CODE)
    assert not d.eligible and "validade não informada/ilegível" in d.reason


def test_expiry_without_event_date_uses_the_pinned_brazilian_today():
    payload = {"status": True, "nome": "X"}
    today = TODAY.strftime("%Y%m%d")
    yesterday = (TODAY - timedelta(days=1)).strftime("%Y%m%d")
    assert interpret({**payload, "dataExpiracao": today}).eligible  # its last day
    assert not interpret({**payload, "dataExpiracao": yesterday}).eligible


# --- a response must be a complete 2xx before it can carry a verdict -------------

APPROVING_DNE_BODY = {"status": True, "nome": "MARIA", "dataExpiracao": "20270331"}


def call_dne(response):
    return check("A", "20000115", "e", "00000000000191", caller=lambda *a, **k: response)


def call_fesn(response, codigo="AB12CD34"):
    return check_fesn(codigo, caller=lambda *a, **k: response)


def test_dne_truncated_response_is_not_validated_and_keeps_the_cause():
    """call_api cuts bodies above 60k chars: the reason must say so, not just "não validada"."""
    body = {"text": "x" * 50, "truncated": True, "hint": "narrow it"}
    d = call_dne(reply(body))
    assert not d.eligible and not d.validated
    assert "truncada/ilegível" in d.reason and "sem decisão" in d.reason
    assert d.raw == {"status": 200, "truncated": True}  # the cut body may hold personal data


def test_dne_unparseable_json_is_not_validated_and_keeps_the_cause():
    body = {"text": "<html>", "parse_error": "content-type said JSON but body did not parse"}
    d = call_dne(reply(body))
    assert not d.eligible and not d.validated
    assert "truncada/ilegível" in d.reason and "JSON inválido" in d.reason
    assert d.raw == {"status": 200, "parse_error": body["parse_error"]}


@pytest.mark.parametrize("status", [301, 404, 503])
def test_dne_non_2xx_is_not_validated_even_with_an_approving_body(status):
    d = call_dne(reply(APPROVING_DNE_BODY, status))
    assert not d.eligible and not d.validated
    assert f"status: {status}" in d.reason and d.holder == {}


def test_dne_response_without_http_status_is_not_validated():
    d = call_dne({"body": APPROVING_DNE_BODY})
    assert not d.eligible and not d.validated and "status: None" in d.reason


def test_dne_refusal_by_the_validator_is_a_verdict_not_a_failure():
    d = call_dne(reply({"status": False, "codigoRetorno": "2"}))
    assert not d.eligible and d.validated


def test_decision_exposes_validated_in_as_dict():
    assert call_dne(reply(APPROVING_DNE_BODY)).as_dict()["validated"] is True
    assert call_dne({"error": "x"}).as_dict()["validated"] is False


def test_fesn_404_with_a_misleading_body_is_not_found_not_approval():
    """FESN answers an unknown code with a 404 error object: `name` is the error class."""
    d = call_fesn(reply({"name": "NotFoundError", "message": "no such code"}, 404))
    assert not d.eligible and d.validated and "não encontrada" in d.reason
    assert d.raw == {"status": 404} and d.holder == {}


@pytest.mark.parametrize("status", [401, 503])
def test_fesn_non_2xx_is_not_validated_even_with_a_perfect_body(status):
    d = call_fesn(reply(FESN_OK, status))
    assert not d.eligible and not d.validated and f"status: {status}" in d.reason


@pytest.mark.parametrize("status,usable", [(199, False), (200, True), (299, True), (300, False)])
def test_only_2xx_responses_can_carry_a_verdict(status, usable):
    for decision in (
        call_dne(reply(APPROVING_DNE_BODY, status)),
        call_fesn(reply(FESN_OK, status)),
    ):
        assert decision.validated is usable and decision.eligible is usable


def test_fesn_response_without_http_status_is_not_validated():
    d = call_fesn({"body": FESN_OK})
    assert not d.eligible and not d.validated


def test_fesn_truncated_response_is_not_validated():
    d = call_fesn(reply({"text": "x", "truncated": True}))
    assert not d.eligible and not d.validated and "truncada/ilegível" in d.reason
    assert d.raw == {"status": 200, "truncated": True}


def test_fesn_unparseable_json_is_not_validated():
    d = call_fesn(reply({"text": "<html>", "parse_error": "bad json"}))
    assert not d.eligible and not d.validated and "truncada/ilegível" in d.reason


def test_fesn_non_dict_body_is_not_validated():
    d = call_fesn(reply("<html>"))
    assert not d.eligible and not d.validated and "inesperada" in d.reason


def test_fesn_gateway_error_is_not_validated():
    d = call_fesn({"error": "ConnectError: boom"})
    assert not d.eligible and not d.validated and "boom" in d.reason


def test_check_any_still_approves_via_fesn_when_dne_is_unreachable():
    def fake_call(api_id, path, **kwargs):
        if api_id == "meiaentrada-cie-validador":
            return {"error": "MEIAENTRADA_CODIGO_ACESSO não definido"}
        return reply(FESN_OK)

    d = check_any("AB12CD34", "1990-01-01", "Show", "00000000000191", caller=fake_call)
    assert d.eligible and d.validated and d.provider == "fesn"


# --- FESN must prove the record is the card we asked for --------------------------


def test_fesn_200_with_an_error_object_is_not_a_card():
    """`name` is the error class in {"name": "NotFoundError"}: it must not read as a holder."""
    d = call_fesn(reply({"name": "NotFoundError", "message": "no such code"}))
    assert not d.eligible and not d.validated and "sem decisão" in d.reason


def test_fesn_200_without_validity_is_refused():
    payload = {k: v for k, v in FESN_OK.items() if k != "validity"}
    d = call_fesn(reply(payload))
    assert not d.eligible and "validade não informada/ilegível" in d.reason


def test_fesn_200_with_a_different_use_code_is_refused_without_echoing_it():
    d = call_fesn(reply({**FESN_OK, "useCode": "ZZ99ZZ99"}))
    assert not d.eligible and not d.validated and d.holder == {} and "sem decisão" in d.reason
    assert "ZZ99ZZ99" not in d.reason  # another person's card code


def test_fesn_200_valid_card_is_approved_end_to_end():
    captured = {}

    def fake_call(api_id, path, method=None, **kwargs):
        captured.update(api_id=api_id, path=path, method=method)
        return reply(FESN_OK)

    d = check_fesn(CODE, "1990-01-01", caller=fake_call, data_evento="2026-12-20")
    assert d.eligible and d.validated and d.provider == "fesn"
    assert captured == {
        "api_id": "fesn-cie-validador",
        "path": "/api/students/usecode/AB12CD34",
        "method": "GET",
    }


def test_fesn_use_code_is_compared_ignoring_case_and_padding():
    assert call_fesn(reply({**FESN_OK, "useCode": "ab12cd34"}), " AB12CD34 ").eligible


@pytest.mark.parametrize("returned", [None, "", "  "])
def test_fesn_never_matches_an_empty_requested_code(returned):
    d = interpret_fesn({"useCode": returned, "validity": "2027-03-31"}, codigo_uso=" ")
    assert not d.eligible


def test_interpret_fesn_cannot_be_called_without_the_requested_code():
    """Making it optional would silently skip the identity check of the record."""
    with pytest.raises(TypeError):
        interpret_fesn(FESN_OK)


def test_fesn_birth_date_supplied_but_not_returned_fails_closed():
    payload = {k: v for k, v in FESN_OK.items() if k != "birth"}
    d = interpret_fesn(payload, "01/01/1990", codigo_uso=CODE)
    assert not d.eligible and "nascimento da FESN não informada/ilegível" in d.reason


def test_fesn_unreadable_birth_date_fails_closed():
    d = interpret_fesn({**FESN_OK, "birth": "N/A"}, "01/01/1990", codigo_uso=CODE)
    assert not d.eligible and "não informada/ilegível" in d.reason


def test_fesn_without_a_supplied_birth_date_does_not_check_identity():
    payload = {k: v for k, v in FESN_OK.items() if k != "birth"}
    assert interpret_fesn(payload, None, codigo_uso=CODE).eligible


def test_fesn_malformed_supplied_birth_date_is_invalid_input():
    with pytest.raises(InvalidInput, match="dataNascimento"):
        interpret_fesn(FESN_OK, "20261399", codigo_uso=CODE)


# --- codigoUso goes into a URL path: it must not be able to change the resource ---


@pytest.mark.parametrize(
    "codigo",
    ["AB/12", "AB?x=1", "AB 12", "../x", ".", "..", "...", "A" * 65, "AB12#f", "ab%2Fcd", "AB\n12"],
)
def test_fesn_rejects_codes_that_would_change_the_requested_resource(codigo):
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("an invalid code must never reach the gateway")

    with pytest.raises(InvalidInput, match="codigoUso"):
        check_fesn(codigo, caller=must_not_be_called)


@pytest.mark.parametrize("codigo", ["AB12CD34", "ab-12_cd.34", "A", "A" * 64, "0000123"])
def test_fesn_accepts_plain_use_codes_and_puts_them_in_the_path_as_is(codigo):
    seen = {}

    def fake_call(api_id, path, **kwargs):
        seen["path"] = path
        return reply({"name": "NotFoundError"}, 404)

    check_fesn(codigo, caller=fake_call)
    assert seen["path"] == f"/api/students/usecode/{codigo}"


# --- check_any: when every base fails, no provider owns the answer ----------------


def dne_and_fesn(dne_response, fesn_response):
    def fake_call(api_id, path, **kwargs):
        return dne_response if api_id == "meiaentrada-cie-validador" else fesn_response

    return fake_call


DNE_NOT_FOUND = reply({"status": False, "codigoRetorno": "2"})
FESN_NOT_FOUND = reply({"name": "NotFoundError"}, 404)


def test_check_any_when_every_base_refuses_has_no_provider_and_both_reasons():
    caller = dne_and_fesn(DNE_NOT_FOUND, FESN_NOT_FOUND)
    d = check_any("X", "20000115", "e", "00000000000191", caller=caller)
    assert not d.eligible and d.validated and d.provider is None
    assert "DNE: documento não encontrado" in d.reason
    assert "FESN: carteirinha não encontrada" in d.reason
    assert "pendência documental" in d.reason and "não como recusa" in d.reason
    assert set(d.raw) == {"dne", "fesn"}  # each base keeps its own diagnosis


def test_check_any_with_one_base_down_is_not_validated():
    """DNE says "not found" but FESN did not answer: the card may well be in FESN."""
    caller = dne_and_fesn(DNE_NOT_FOUND, reply("Bad Gateway", 502))
    d = check_any("X", "20000115", "e", "00000000000191", caller=caller)
    assert not d.eligible and not d.validated and d.provider is None
    assert "DNE:" in d.reason and "FESN: validador não respondeu com HTTP 2xx" in d.reason


def test_check_any_with_both_bases_down_is_not_validated_and_names_both():
    caller = dne_and_fesn({"error": "ConnectError: dne"}, {"error": "ConnectError: fesn"})
    d = check_any("X", "20000115", "e", "00000000000191", caller=caller)
    assert not d.eligible and not d.validated and d.provider is None
    assert "(DNE: falha na chamada" in d.reason and "; FESN: falha na chamada" in d.reason


def test_check_any_returns_the_dne_approval_without_asking_fesn():
    def fake_call(api_id, path, **kwargs):
        assert api_id == "meiaentrada-cie-validador"
        return reply({"status": True, "nome": "MARIA", "dataExpiracao": "20270331"})

    d = check_any("ABC", "20000115", "e", "00000000000191", "2026-12-20", fake_call)
    assert d.eligible and d.provider == "dne"


# --- a refusal must not hand the card holder's data back to whoever asked ---------

PERSONAL_DATA = ("FULANA DE TAL", "00000000000", "1990-01-01", "example.invalid", "ENGENHARIA")
DNE_FULL_PAYLOAD = {
    "status": True,
    "codigoRetorno": "1",
    "mensagem": "documento válido",
    "message": "ok",
    "nome": "MARIA DA SILVA SANTOS",
    "cpf": "123.456.789-00",
    "documento": "MG-11.111.111",
    "urlFoto": "https://example.invalid/foto.jpg",
    "dataExpiracao": "20260930",
}


def assert_no_personal_data(decision, extra=()):
    """Nothing the caller can serialize, log or show may carry the holder's data."""
    dumped = json.dumps(decision.as_dict(), ensure_ascii=False)
    for secret in (*PERSONAL_DATA, *extra):
        assert secret not in dumped, secret


def test_fesn_wrong_birth_date_does_not_hand_back_the_real_one():
    """A wrong guess must not return the real birth date, CPF and name through raw."""
    d = interpret_fesn(FESN_OK, "1990-01-02", codigo_uso=CODE)
    assert not d.eligible and d.holder == {} and "não confere" in d.reason
    assert_no_personal_data(d)
    assert d.raw == {"validity": "2027-03-31 23:59:59"}


def test_fesn_expired_card_keeps_no_personal_data():
    d = interpret_fesn({**FESN_OK, "validity": "2020-03-31 23:59:59"}, codigo_uso=CODE)
    assert not d.eligible and "vence em" in d.reason
    assert_no_personal_data(d)


def test_fesn_unreadable_birth_date_keeps_no_personal_data():
    d = interpret_fesn({**FESN_OK, "birth": "N/A"}, "01/01/1990", codigo_uso=CODE)
    assert not d.eligible
    assert_no_personal_data(d)


def test_fesn_record_of_another_card_keeps_no_personal_data():
    d = interpret_fesn(FESN_OK, None, codigo_uso="ZZ99ZZ99")
    assert not d.eligible and "sem decisão" in d.reason
    assert_no_personal_data(d)


def test_dne_expired_card_keeps_only_non_personal_keys():
    d = interpret(DNE_FULL_PAYLOAD, data_evento="2026-12-20")
    assert not d.eligible and "antes da data do evento" in d.reason
    assert d.raw == {
        "status": True,
        "codigoRetorno": "1",
        "mensagem": "documento válido",
        "message": "ok",
        "dataExpiracao": "20260930",
    }
    assert_no_personal_data(d, extra=("MARIA DA SILVA", "123.456.789-00", "MG-11.111.111"))


def test_dne_unreadable_expiry_keeps_no_personal_data():
    payload = {k: v for k, v in DNE_FULL_PAYLOAD.items() if k != "dataExpiracao"}
    d = interpret(payload, data_evento="2026-12-20")
    assert not d.eligible and "validade não informada/ilegível" in d.reason
    assert_no_personal_data(d, extra=("MARIA DA SILVA", "123.456.789-00"))


def test_dne_refusal_keeps_the_verdict_and_drops_everything_else():
    payload = {
        "status": False,
        "codigoRetorno": "2",
        "mensagem": "documento inexistente",
        "nome": "LEAK",
        "cpf": "999.999.999-99",
    }
    d = interpret(payload)
    assert d.raw == {"status": False, "codigoRetorno": "2", "mensagem": "documento inexistente"}


def test_eligible_decisions_keep_the_full_payload():
    approved = {**DNE_FULL_PAYLOAD, "dataExpiracao": "20270331"}
    assert interpret(approved, data_evento="2026-12-20").raw == approved
    assert interpret_fesn(FESN_OK, None, "2026-12-20", codigo_uso=CODE).raw == FESN_OK


def test_check_any_combined_decision_keeps_no_personal_data():
    caller = dne_and_fesn(DNE_NOT_FOUND, reply(FESN_OK))
    d = check_any("AB12CD34", "1990-01-02", "e", "00000000000191", caller=caller)
    assert not d.eligible and d.provider is None
    assert_no_personal_data(d)


# --- the time after a date must be a real time, not just "something" ----------------


@pytest.mark.parametrize(
    "value",
    [
        "2027-03-31 23:59:59",
        "2027-03-31T23:59",
        "2027-03-31T23:59:59.123Z",
        "2027-03-31T23:59:59-03:00",
        "2027-03-31 00:00:00+0000",
        " 2027-03-31 ",
    ],
)
def test_parse_date_accepts_a_real_time_after_the_date(value):
    assert parse_date(value) == date(2027, 3, 31)


@pytest.mark.parametrize(
    "value",
    [
        "2027-03-31 9x",
        "2027-03-31T99:99",
        "2027-03-31T24:00",
        "2027-03-31T23:60",
        "2027-03-31T",
        "2027-03-31 23:59:59 extra",
        "2027-03-31T10:00+99:99",
    ],
)
def test_parse_date_rejects_anything_but_a_real_time_after_the_date(value):
    assert parse_date(value) is None


# --- FESN 404 is "not found" only with the error object it is expected to carry ------


@pytest.mark.parametrize(
    "body",
    [
        "<html>502 Bad Gateway</html>",
        "Not Found",
        None,
        {"message": "no such code"},
        {"name": "SomethingElse"},
        {"text": "x", "truncated": True},
    ],
)
def test_fesn_404_without_the_expected_error_object_is_not_validated(body):
    """A proxy or WAF page can answer 404 too; it says nothing about the card."""
    d = call_fesn(reply(body, 404))
    assert not d.eligible and not d.validated and "status: 404" in d.reason


# --- no verdict on the card = not validated, never "not eligible" ----------------------


def test_dne_code_5_is_our_request_out_of_contract_not_a_verdict():
    d = interpret({"status": False, "codigoRetorno": "5", "mensagem": "parâmetros inválidos"})
    assert not d.eligible and not d.validated
    assert "parâmetros inválidos" in d.reason and "sem decisão" in d.reason


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"nome": None},
        {"mensagem": "x"},
        {"status": None, "codigoRetorno": None},
        {"status": "talvez"},
        {"codigoRetorno": ""},
    ],
)
def test_dne_payload_without_status_or_return_code_is_not_a_verdict(payload):
    d = interpret(payload)
    assert not d.eligible and not d.validated and "sem veredito" in d.reason


@pytest.mark.parametrize(
    "payload",
    [
        {"status": False},
        {"status": "false"},
        {"codigoRetorno": "2"},
        {"status": None, "codigoRetorno": 2},
        {"status": False, "codigoRetorno": "99", "mensagem": "algo novo"},
    ],
)
def test_dne_payload_with_a_status_or_return_code_is_a_verdict(payload):
    d = interpret(payload)
    assert not d.eligible and d.validated


def test_dne_200_without_a_verdict_is_not_validated():
    d = call_dne(reply({}))
    assert not d.eligible and not d.validated and d.provider == "dne"


def test_dne_code_5_through_check_is_not_validated():
    d = call_dne(reply({"status": False, "codigoRetorno": "5"}))
    assert not d.eligible and not d.validated and d.provider == "dne"


def test_fesn_200_with_an_empty_object_is_not_validated():
    d = call_fesn(reply({}))
    assert not d.eligible and not d.validated


def test_check_any_with_dne_code_5_is_not_validated():
    caller = dne_and_fesn(reply({"status": False, "codigoRetorno": "5"}), FESN_NOT_FOUND)
    d = check_any("X", "20000115", "e", "00000000000191", caller=caller)
    assert not d.eligible and not d.validated and d.provider is None


# --- input is validated before the first network call, whichever base needs it -------


def never_called(*args, **kwargs):
    raise AssertionError("invalid input must be rejected before any network call")


@pytest.mark.parametrize(
    "kwargs",
    [{"data_nascimento": "20261399"}, {"data_nascimento": ""}, {"data_evento": "2026-13-45"}],
)
def test_fesn_validates_birth_and_event_dates_before_the_call(kwargs):
    with pytest.raises(InvalidInput):
        check_fesn(CODE, caller=never_called, **kwargs)


@pytest.mark.parametrize("codigo", ["AB/12", "", "A" * 65])
def test_check_any_validates_the_fesn_code_before_asking_dne(codigo):
    """A late InvalidInput from the FESN leg would discard the DNE verdict, so nothing is
    asked before every base's input is known to be valid."""
    with pytest.raises(InvalidInput, match="codigoUso"):
        check_any(codigo, "20000115", "e", "00000000000191", caller=never_called)


def test_check_any_validates_the_dne_fields_before_the_first_call():
    with pytest.raises(InvalidInput, match="cnpjPromotor"):
        check_any(CODE, "20000115", "e", "123", caller=never_called)


def test_fesn_empty_birth_date_is_invalid_input_not_a_skipped_check():
    with pytest.raises(InvalidInput, match="dataNascimento"):
        interpret_fesn(FESN_OK, "", codigo_uso=CODE)
