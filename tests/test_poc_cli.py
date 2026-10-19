"""CLI contract of poc/validar_cie.py: exit codes and machine-readable output.

Nothing here touches the network: the demo is offline, and every other run replaces the
gateway (`server.call_api`) with a stub.
"""

import errno
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


def dne_argv(codigo_uso="ABC123", nascimento="1999-04-21", cnpj="00.000.000/0001-91"):
    return ["--codigo-uso", codigo_uso, "--nascimento", nascimento, "--cnpj", cnpj]


DNE_ARGS = dne_argv()


def reply(body, status=200):
    return {"status": status, "url": "https://example.invalid/x", "body": body}


class UnexpectedGatewayCall(BaseException):
    """Not an Exception on purpose: the POC turns every Exception into exit 3, which would
    hide a call that must not happen behind a test that expects exit 3."""


def gateway(monkeypatch, dne=None, fesn=None):
    """Replace server.call_api; a base left as None must never be called."""

    def call_api(api_id, path, **kwargs):
        response = dne if api_id == DNE else fesn
        if response is None:
            raise UnexpectedGatewayCall(api_id)
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
    "argv,field",
    [
        (["--provedor", "dne", *dne_argv(nascimento="20261399")], "dataNascimento"),
        (["--provedor", "dne", *dne_argv(cnpj="123")], "cnpjPromotor"),
        (["--provedor", "fesn", "--codigo-uso", "AB/12"], "codigoUso"),
        (["--provedor", "fesn", "--codigo-uso", "AB12CD34", "--nascimento", ""], "dataNascimento"),
        (
            ["--provedor", "fesn", "--codigo-uso", "AB12CD34", "--data-evento", "2026-13-45"],
            "dataEvento",
        ),
        (["--provedor", "fesn", "--codigo-uso", "AB12CD34", "--data-evento", ""], "dataEvento"),
        (["--provedor", "fesn", "--codigo-uso", "AB12CD34", "--data-evento", "  "], "dataEvento"),
        (["--provedor", "dne", *dne_argv(), "--data-evento", ""], "dataEvento"),
        (["--provedor", "auto", *dne_argv(), "--data-evento", ""], "dataEvento"),
    ],
)
def test_invalid_input_exits_2_names_the_field_and_never_reaches_the_gateway(
    poc, monkeypatch, capsys, argv, field
):
    gateway(monkeypatch)  # neither base may be called
    assert poc.main(argv) == 2
    err = capsys.readouterr().err
    assert "entrada inválida" in err and field in err


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

APPROVED = reply(
    {"status": True, "codigoRetorno": "1", "nome": "MARIA", "dataExpiracao": "20270331"}
)
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
        (reply({"status": False, "codigoRetorno": "99", "mensagem": "x"}), 3),  # unknown code
        (reply({"status": True, "codigoRetorno": "2", "dataExpiracao": "20270331"}), 3),
        (
            reply(
                {"status": True, "codigoRetorno": "1", "nome": "MARIA", "dataExpiracao": "20260930"}
            ),
            1,
        ),  # expired
        (
            reply({"status": True, "codigoRetorno": "1", "nome": "MARIA"}),
            1,
        ),  # no validity to judge by
        (reply({"status": True, "dataExpiracao": "20270331"}), 3),  # true status, no code at all
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


@pytest.mark.parametrize(
    "fesn,extra,expected",
    [
        (reply(FESN_CARD), [], 0),
        (reply({**FESN_CARD, "validity": "2026-09-30 23:59:59"}), [], 1),  # gone before the event
        (reply(FESN_CARD), ["--nascimento", "1990-01-02"], 1),  # birth date does not match
        (
            reply({k: v for k, v in FESN_CARD.items() if k != "birth"}),
            ["--nascimento", "1990-01-01"],
            1,  # FESN gave no birth date to compare
        ),
        (FESN_UNKNOWN, [], 1),
        (reply("<html>WAF</html>", 404), [], 3),
        (reply({"name": "NotFoundError"}), [], 3),
        (reply("Bad Gateway", 502), [], 3),
    ],
)
def test_fesn_exit_codes(poc, monkeypatch, fesn, extra, expected):
    gateway(monkeypatch, fesn=fesn)
    argv = ["--provedor", "fesn", "--codigo-uso", "AB12CD34", "--data-evento", "2026-12-20"]
    assert poc.main([*argv, *extra]) == expected


# --- in auto, a code that is not a FESN token still gets the DNE's answer ------------------------------------


@pytest.mark.parametrize("dne,expected", [(APPROVED, 0), (NOT_FOUND, 1), ({"error": "boom"}, 3)])
def test_auto_with_a_code_that_is_not_a_fesn_token_asks_only_dne(poc, monkeypatch, dne, expected):
    gateway(monkeypatch, dne=dne)  # asking FESN would raise
    argv = ["--provedor", "auto", "--data-evento", "2026-12-20", *dne_argv(codigo_uso="AB/12")]
    assert poc.main(argv) == expected


# --- what the CLI sends to the DNE ------------------------------------------------------------------------


def test_event_name_cnpj_and_date_reach_the_dne_request(poc, monkeypatch):
    seen = {}

    def call_api(api_id, path, method=None, body=None, **kwargs):
        seen["body"] = body
        return NOT_FOUND

    monkeypatch.setattr(server, "call_api", call_api)
    argv = ["--provedor", "dne", "--evento", "Show Z", "--data-evento", "2026-12-20", *DNE_ARGS]
    assert poc.main(argv) == 1
    assert seen["body"]["nomeEvento"] == "Show Z"
    assert seen["body"]["dataEvento"] == "20261220"
    assert seen["body"]["cnpjPromotor"] == "00000000000191"


# --- the output phase is part of the run: a failure there is exit 3, not a traceback ------------------------


class StdoutThatCannotEncode:
    """A stdout whose encoding has no em dash, like the reasons carry."""

    def write(self, text):
        raise UnicodeEncodeError("latin-1", text, 0, 1, "ordinal not in range(256)")

    def flush(self):
        pass


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_failure_while_printing_a_decision_exits_3(poc, monkeypatch, capsys, flags):
    gateway(monkeypatch, dne=APPROVED)
    monkeypatch.setattr(sys, "stdout", StdoutThatCannotEncode())
    argv = ["--provedor", "dne", "--data-evento", "2026-12-20", *DNE_ARGS, *flags]
    assert poc.main(argv) == 3
    assert "falha inesperada (UnicodeEncodeError)" in capsys.readouterr().err


def test_failure_while_printing_the_demo_exits_3(poc, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdout", StdoutThatCannotEncode())
    assert poc.main(["--demo"]) == 3
    assert "falha inesperada (UnicodeEncodeError)" in capsys.readouterr().err


# --- --help ----------------------------------------------------------------------------------------------------


def test_help_keeps_the_example_command_on_its_two_lines():
    """The docstring is shown as is; without a raw string the trailing backslash is eaten."""
    result = run_cli("--help")
    assert result.returncode == 0
    assert "--nascimento 1999-04-21 \\\n" in result.stdout


def test_help_says_what_exit_3_covers():
    helptext = run_cli("--help").stdout
    for fragment in ("exceção inesperada", "codigoRetorno 5 ou desconhecido", "sem veredito"):
        assert fragment in helptext


def test_help_limits_the_missing_credential_exit_to_the_dne_provider():
    assert "credencial ausente com --provedor dne" in run_cli("--help").stdout


# --- the credential exit is the DNE provider's alone ---------------------------------------------------------


def test_missing_credential_is_exit_2_only_with_the_dne_provider(poc, monkeypatch):
    monkeypatch.delenv("MEIAENTRADA_CODIGO_ACESSO")
    no_key = {"error": "meiaentrada-cie-validador requires env var MEIAENTRADA_CODIGO_ACESSO"}
    fesn_argv = ["--provedor", "fesn", "--codigo-uso", "AB12CD34", "--data-evento", "2026-12-20"]
    auto_argv = [
        *["--provedor", "auto", "--data-evento", "2026-12-20"],
        *dne_argv(codigo_uso="AB12CD34", nascimento="1990-01-01"),  # the FESN card's birth date
    ]

    gateway(monkeypatch, fesn=reply(FESN_CARD))  # FESN never needed it
    assert poc.main(fesn_argv) == 0
    gateway(monkeypatch, dne=no_key, fesn=reply(FESN_CARD))  # auto: the DNE leg fails, FESN decides
    assert poc.main(auto_argv) == 0
    gateway(monkeypatch, dne=no_key, fesn=FESN_UNKNOWN)
    assert poc.main(auto_argv) == 3  # a failed leg, not bad input
    gateway(monkeypatch)  # --provedor dne stops before any request
    assert poc.main(["--provedor", "dne", *DNE_ARGS]) == 2


# --- a stdout that fails with an I/O error must not turn our exit code into 120 -----------------------------


class StdoutWithABrokenPipe:
    """A stdout over a pipe nobody reads: write() or flush() fails with EPIPE."""

    def __init__(self, fd, fail_on):
        self.fd, self.fail_on = fd, fail_on

    def _maybe_fail(self, operation):
        if operation == self.fail_on:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def write(self, text):
        self._maybe_fail("write")
        return len(text)

    def flush(self):
        self._maybe_fail("flush")

    def fileno(self):
        return self.fd


@pytest.mark.parametrize("fail_on", ["write", "flush"])
def test_broken_stdout_exits_3_and_points_its_fd_at_devnull(poc, monkeypatch, capsys, fail_on):
    gateway(monkeypatch, dne=APPROVED)
    reader, writer = os.pipe()
    os.close(reader)  # nobody reads this pipe: writing to it fails with EPIPE
    try:
        monkeypatch.setattr(sys, "stdout", StdoutWithABrokenPipe(writer, fail_on))
        argv = ["--provedor", "dne", "--data-evento", "2026-12-20", *DNE_ARGS]
        assert poc.main(argv) == 3
        assert "falha inesperada (BrokenPipeError)" in capsys.readouterr().err
        # The interpreter flushes stdout once more at exit: that write must not fail again.
        assert os.write(writer, b"x") == 1
    finally:
        os.close(writer)


def test_a_flush_that_fails_is_part_of_the_run_not_of_the_shutdown(poc, monkeypatch, capsys):
    """Buffered output only fails when flushed; the flush has to happen inside the try."""
    gateway(monkeypatch, dne=NOT_FOUND)
    monkeypatch.setattr(sys, "stdout", StdoutWithABrokenPipe(-1, "flush"))
    assert poc.main(["--provedor", "dne", "--data-evento", "2026-12-20", *DNE_ARGS]) == 3
    assert "falha inesperada (BrokenPipeError)" in capsys.readouterr().err


def test_closed_stdout_pipe_exits_3_not_120():
    """The real thing: the interpreter's flush at exit fails too, unless fd 1 was redirected."""
    reader, writer = os.pipe()
    os.close(reader)  # closed before the child starts: the first flush is guaranteed to fail
    try:
        env = {k: v for k, v in os.environ.items() if k != "MEIAENTRADA_CODIGO_ACESSO"}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--demo"],
            check=False,
            stdout=writer,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            env=env,
        )
    finally:
        os.close(writer)
    assert result.returncode == 3, result.stderr
    assert "falha inesperada (BrokenPipeError)" in result.stderr
    assert "Exception ignored" not in result.stderr
