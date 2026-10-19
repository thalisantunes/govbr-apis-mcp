"""CLI contract of poc/validar_cie.py: exit codes and machine-readable output.

Nothing here touches the network: the demo is offline, and every other run replaces the
gateway (`server.call_api`) with a stub.
"""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from govbr_apis_mcp import cie, server

SCRIPT = Path(__file__).resolve().parents[1] / "poc" / "validar_cie.py"
DNE = "meiaentrada-cie-validador"
FESN_CARD = {
    "useCode": "AB12CD34",
    "name": "FULANA DE TAL",
    "birth": "1990-01-01T00:00:00",
    "cpf": "00000000000",
    "entity": "IFRS",
    "validity": "2027-03-31 23:59:59",
}
DNE_ARGS = ["--codigo-uso", "ABC123", "--nascimento", "1999-04-21", "--cnpj", "00.000.000/0001-91"]


def reply(body, status=200):
    return {"status": status, "url": "https://example.invalid/x", "body": body}


def gateway(monkeypatch, dne=None, fesn=None):
    """Replace server.call_api; a base left as None must never be called."""

    def call_api(api_id, path, **kwargs):
        response = dne if api_id == DNE else fesn
        assert response is not None, f"unexpected call to {api_id}"
        return response

    monkeypatch.setattr(server, "call_api", call_api)


@pytest.fixture(scope="module")
def poc():
    spec = importlib.util.spec_from_file_location("validar_cie", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def pinned_environment(monkeypatch):
    """No test reads the real clock or the real credential."""
    monkeypatch.setattr(cie, "today_br", lambda: date(2026, 8, 7))
    monkeypatch.setenv("MEIAENTRADA_CODIGO_ACESSO", "test-only")


def run_cli(*args):
    env = {k: v for k, v in os.environ.items() if k != "MEIAENTRADA_CODIGO_ACESSO"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


# --- the demo: offline, deterministic, valid JSON ----------------------------------


def test_demo_json_is_a_single_valid_document():
    result = run_cli("--demo", "--json")
    assert result.returncode == 0, result.stderr
    cases = json.loads(result.stdout)  # the whole stdout, not just its first object
    assert list(cases) == ["aprovado", "inexistente_ou_data_errada", "payload_errado"]
    assert cases["aprovado"]["eligible"] is True and cases["aprovado"]["validated"] is True
    assert cases["inexistente_ou_data_errada"]["validated"] is True  # a verdict: not found
    # code 5 is our payload out of contract: no verdict on the card, so not "not eligible"
    assert cases["payload_errado"]["eligible"] is False
    assert cases["payload_errado"]["validated"] is False


def test_demo_text_mode_runs_offline_and_exits_zero():
    result = run_cli("--demo")
    assert result.returncode == 0, result.stderr
    assert "=== aprovado: ELEGIVEL ===" in result.stdout
    assert "=== inexistente_ou_data_errada: NAO ELEGIVEL ===" in result.stdout
    assert "=== payload_errado: NAO VALIDADO ===" in result.stdout


def test_demo_never_reads_the_clock(poc, monkeypatch):
    """The recorded approval expires on 2027-03-31: a demo using "today" would rot."""

    def clock_must_not_be_read():
        raise AssertionError("the demo has a fixed event date")

    monkeypatch.setattr(cie, "today_br", clock_must_not_be_read)
    assert poc.main(["--demo", "--json"]) == 0


# --- arguments ----------------------------------------------------------------------


def test_missing_arguments_exit_2_and_name_them():
    result = run_cli("--codigo-uso", "ABC123")
    assert result.returncode == 2
    assert "--nascimento" in result.stderr and "--cnpj" in result.stderr


def test_fesn_needs_only_the_use_code(poc, monkeypatch, capsys):
    gateway(monkeypatch, fesn=reply(FESN_CARD))
    argv = ["--provedor", "fesn", "--codigo-uso", "AB12CD34", "--data-evento", "2026-12-20"]
    assert poc.main(argv) == 0
    assert "ELEGIVEL" in capsys.readouterr().out


def test_dne_still_requires_birth_date_and_cnpj(poc, capsys):
    with pytest.raises(SystemExit) as exc:
        poc.main(["--provedor", "dne", "--codigo-uso", "ABC123"])
    assert exc.value.code == 2


def test_dne_without_credential_exits_2(poc, monkeypatch, capsys):
    monkeypatch.delenv("MEIAENTRADA_CODIGO_ACESSO")
    assert poc.main(["--provedor", "dne", *DNE_ARGS]) == 2
    assert "MEIAENTRADA_CODIGO_ACESSO" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--provedor", "dne", "--codigo-uso", "ABC123", "--nascimento", "20261399", "--cnpj", "1"],
        [
            "--provedor",
            "dne",
            "--codigo-uso",
            "ABC123",
            "--nascimento",
            "1999-04-21",
            "--cnpj",
            "1",
        ],
        ["--provedor", "fesn", "--codigo-uso", "AB/12"],
    ],
)
def test_invalid_input_exits_2(poc, argv, capsys):
    assert poc.main(argv) == 2
    assert "entrada inválida" in capsys.readouterr().err


# --- --data-evento reaches every provider -------------------------------------------


def test_fesn_honours_the_event_date(poc, monkeypatch, capsys):
    """A card valid until 2026-09-30 is fine today (2026-08-07) but not for a December show."""
    gateway(monkeypatch, fesn=reply({**FESN_CARD, "validity": "2026-09-30 23:59:59"}))
    base = ["--provedor", "fesn", "--codigo-uso", "AB12CD34"]
    assert poc.main(base) == 0
    assert poc.main([*base, "--data-evento", "2026-12-20"]) == 1
    assert "antes da data do evento" in capsys.readouterr().out


def test_auto_and_fesn_agree_on_the_same_expired_card(poc, monkeypatch):
    expired_fesn = reply({**FESN_CARD, "validity": "2026-09-30 23:59:59"})
    gateway(monkeypatch, dne=reply({"status": False, "codigoRetorno": "2"}), fesn=expired_fesn)
    event = ["--data-evento", "2026-12-20", "--codigo-uso", "AB12CD34"]
    auto = poc.main(
        ["--provedor", "auto", "--nascimento", "1990-01-01", "--cnpj", "1" * 14, *event]
    )
    fesn = poc.main(["--provedor", "fesn", "--nascimento", "1990-01-01", *event])
    assert auto == fesn == 1


# --- exit codes: 0 eligible, 1 not eligible, 3 not validated -------------------------

APPROVED = reply({"status": True, "nome": "MARIA", "dataExpiracao": "20270331"})
NOT_FOUND = reply({"status": False, "codigoRetorno": "2"})
FESN_UNKNOWN = reply({"name": "NotFoundError"}, 404)


@pytest.mark.parametrize(
    "dne,expected",
    [
        (APPROVED, 0),
        (NOT_FOUND, 1),
        ({"error": "ConnectError: boom"}, 3),
        (reply("Bad Gateway", 502), 3),
        (reply({"text": "x", "truncated": True}), 3),
        (reply({"status": False, "codigoRetorno": "5"}), 3),  # our payload out of contract
        (reply({}), 3),  # a 200 that says nothing
    ],
)
def test_dne_exit_codes(poc, monkeypatch, dne, expected):
    gateway(monkeypatch, dne=dne)
    assert poc.main(["--provedor", "dne", "--data-evento", "2026-12-20", *DNE_ARGS]) == expected


@pytest.mark.parametrize(
    "dne,fesn,expected",
    [
        (NOT_FOUND, reply(FESN_CARD), 0),  # unknown to DNE, valid in FESN
        (NOT_FOUND, FESN_UNKNOWN, 1),  # asked everywhere: documents, not a verdict of fraud
        ({"error": "ConnectError: boom"}, FESN_UNKNOWN, 3),  # DNE unreachable: card may be there
        (NOT_FOUND, reply("Bad Gateway", 502), 3),  # FESN unreachable
        (NOT_FOUND, reply("<html>WAF</html>", 404), 3),  # a 404 that is not FESN's error object
        (NOT_FOUND, reply({"name": "NotFoundError"}), 3),  # a 200 without the card's useCode
    ],
)
def test_auto_exit_codes(poc, monkeypatch, dne, fesn, expected):
    gateway(monkeypatch, dne=dne, fesn=fesn)
    argv = ["--provedor", "auto", "--data-evento", "2026-12-20", "--nascimento", "1990-01-01"]
    assert poc.main([*argv, "--codigo-uso", "AB12CD34", "--cnpj", "1" * 14]) == expected


def test_a_crash_is_not_a_verdict_it_exits_3_with_the_cause_on_stderr(poc, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "call_api", boom)
    for provider in ("dne", "auto"):
        assert poc.main(["--provedor", provider, *DNE_ARGS]) == 3
        captured = capsys.readouterr()
        assert "falha inesperada (RuntimeError): boom" in captured.err
        assert captured.out == ""


def test_not_validated_is_labelled_and_serialised_as_such(poc, monkeypatch, capsys):
    gateway(monkeypatch, dne=reply("Bad Gateway", 502))
    assert poc.main(["--provedor", "dne", "--json", *DNE_ARGS]) == 3
    decision = json.loads(capsys.readouterr().out)
    assert decision["eligible"] is False and decision["validated"] is False

    assert poc.main(["--provedor", "dne", *DNE_ARGS]) == 3
    assert "NAO VALIDADO" in capsys.readouterr().out
