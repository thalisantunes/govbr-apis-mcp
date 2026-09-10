import pytest

from govbr_apis_mcp.cie import (
    InvalidInput,
    build_payload,
    check,
    check_any,
    interpret,
    interpret_fesn,
    normalize_date,
)


@pytest.mark.parametrize(
    "value,expected",
    [("20000115", "20000115"), ("2000-01-15", "20000115"), ("15/01/2000", "20000115")],
)
def test_normalize_date_accepts_common_formats(value, expected):
    assert normalize_date(value, "dataNascimento") == expected


@pytest.mark.parametrize("value", ["15012000x", "2000", "", "não é data"])
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
        }
    )
    assert d.eligible
    assert d.holder["nome"] == "MARIA" and d.holder["has_photo"] is True
    assert "curso" not in d.holder  # campos nulos não poluem a conferência


def test_interpret_accepts_status_as_string():
    """The API returns the string "true" in some paths — the front-end handles both."""
    assert interpret({"status": "true", "nome": "X"}).eligible


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


def test_check_handles_non_dict_body():
    d = check("A", "20000115", "e", "00000000000191", caller=lambda *a, **k: {"body": "<html>"})
    assert not d.eligible and "inesperada" in d.reason


def test_check_sends_expected_request():
    captured = {}

    def fake_call(api_id, path, method=None, body=None, **kwargs):
        captured.update(api_id=api_id, path=path, method=method, body=body)
        return {"body": {"status": True, "nome": "MARIA"}}

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
    d = interpret_fesn(FESN_OK, "01/01/1990")
    assert d.eligible and d.provider == "fesn"
    assert d.holder["instituicao"] == "IFRS" and d.holder["has_photo"] is True


def test_fesn_rejects_wrong_birth_date():
    """FESN validates from the use code alone, so we check identity ourselves."""
    d = interpret_fesn(FESN_OK, "1990-01-02")
    assert not d.eligible and "não confere" in d.reason


def test_fesn_rejects_expired_card():
    """`validity` comes in the payload but the API never enforces it."""
    d = interpret_fesn({**FESN_OK, "validity": "2020-03-31 23:59:59"}, None)
    assert not d.eligible and "vence em" in d.reason


def test_fesn_unknown_card():
    assert not interpret_fesn({}, None).eligible


def test_check_any_falls_back_to_fesn(monkeypatch):
    """A legitimate card unknown to DNE must not be denied — the real AB12CD34 case."""
    def fake_call(api_id, path, **kwargs):
        if api_id == "meiaentrada-cie-validador":
            return {"body": {"status": False, "codigoRetorno": "2"}}
        return {"body": FESN_OK}

    d = check_any("AB12CD34", "1990-01-01", "Show", "00000000000191", caller=fake_call)
    assert d.eligible and d.provider == "fesn"


def test_check_any_reason_when_all_bases_fail():
    d = check_any("X", "20000115", "e", "00000000000191",
                  caller=lambda *a, **k: {"body": {"status": False, "codigoRetorno": "2"}})
    assert not d.eligible
    assert "pendência documental" in d.reason and "não como recusa" in d.reason


def test_expiry_is_checked_against_event_date_not_today():
    """A card expiring before the show must not approve a purchase made today."""
    d = interpret_fesn({**FESN_OK, "validity": "2026-09-30 23:59:59"}, None, "2026-12-20")
    assert not d.eligible and "antes da data do evento" in d.reason


def test_card_valid_through_event_date_passes():
    d = interpret_fesn({**FESN_OK, "validity": "2027-03-31 23:59:59"}, None, "2026-12-20")
    assert d.eligible


def test_dne_expiry_enforced_locally():
    """We never trust the DNE `status` alone for expiry — undocumented behaviour."""
    d = interpret(
        {"status": True, "nome": "X", "dataExpiracao": "2026-09-30"}, data_evento="2026-12-20"
    )
    assert not d.eligible and "antes da data do evento" in d.reason


def test_check_any_propagates_event_date_to_fesn():
    def fake_call(api_id, path, **kwargs):
        if api_id == "meiaentrada-cie-validador":
            return {"body": {"status": False, "codigoRetorno": "2"}}
        return {"body": {**FESN_OK, "validity": "2026-09-30 23:59:59"}}

    d = check_any("AB12CD34", "1990-01-01", "Show", "00000000000191", "2026-12-20", fake_call)
    assert not d.eligible
