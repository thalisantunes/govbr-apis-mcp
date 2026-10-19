#!/usr/bin/env python3
r"""POC: valida meia-entrada de estudante (CIE) na compra ou na portaria.

Uso real (precisa de MEIAENTRADA_CODIGO_ACESSO e de uma CIE de verdade):

    python poc/validar_cie.py --codigo-uso ABC123 --nascimento 1999-04-21 \
        --evento "Show X" --cnpj 00.000.000/0001-91

Sem credencial nem carteirinha à mão, o modo demo roda a mesma validação (cie.check)
com respostas de exemplo no lugar da chamada de rede (as respostas negativas foram
gravadas da API real; a aprovação é sintética):

    python poc/validar_cie.py --demo

Exit code: 0 elegível; 1 não elegível (os validadores responderam e a carteirinha não
vale); 2 entrada inválida, ou credencial ausente com --provedor dne; 3 não validado
(faltou resposta utilizável para decidir: rede ou gateway, HTTP fora de 2xx, resposta
truncada, ilegível ou sem veredito, codigoRetorno 5 ou desconhecido, exceção inesperada,
inclusive ao imprimir; em auto, também quando uma base não encontrou a carteirinha e a
outra falhou). O 3 nunca é recusa: tente de novo ou peça comprovação documental.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from govbr_apis_mcp.cie import InvalidInput, check, check_any, check_fesn

# Respostas reais da API, capturadas em 2026-08-07. O caso aprovado é sintético:
# reproduz o shape documentado sem expor dados de um estudante real.
DEMO_CASES = {
    "aprovado": {
        "status": True,
        "codigoRetorno": "1",
        "mensagem": "documento válido",
        "nome": "MARIA DA SILVA SANTOS",
        "nomeSocial": None,
        "cpf": "123.456.789-00",
        "documento": "MG-11.111.111",
        "instituicao": "UNIVERSIDADE FEDERAL DE MINAS GERAIS",
        "curso": "CIENCIA DA COMPUTACAO",
        "nivelEscolaridade": "SUPERIOR",
        "entidade": "UNE",
        "municipio": "BELO HORIZONTE",
        "certificado": "CIE-2026-0001234",
        "dataExpiracao": "20270331",
        "urlFoto": "https://www.meiaentrada.org.br/foto/exemplo.jpg",
    },
    "inexistente_ou_data_errada": {
        "status": False,
        "codigoRetorno": "2",
        "mensagem": "documento inexistente",
        "nome": None,
    },
    "payload_errado": {
        "status": False,
        "codigoRetorno": "5",
        "mensagem": "parâmetros inválidos",
        "nome": None,
    },
}
# Fixa de propósito: a carteirinha aprovada vence em 2027-03-31, então um demo que usasse
# "hoje" passaria a recusá-la em 2027-04-01.
DEMO_DATA_EVENTO = "2026-12-20"


def recorded(payload):
    """Gateway de mentira que responde sempre `payload`, no formato do call_api."""
    return lambda *args, **kwargs: {"status": 200, "body": payload}


def render(decision, titulo: str) -> None:
    if decision.eligible:
        mark = "ELEGIVEL"
    else:
        mark = "NAO ELEGIVEL" if decision.validated else "NAO VALIDADO"
    print(f"\n=== {titulo}: {mark} ===")
    print(f"motivo: {decision.reason}")
    if decision.provider:
        print(f"base: {decision.provider}")
    for key, value in decision.holder.items():
        print(f"  {key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--codigo-uso",
        help="código de uso da CIE (o que vem no QR code); em auto, a FESN só é consultada se "
        "ele for um token de 1 a 64 letras, dígitos, '.', '_' ou '-'",
    )
    parser.add_argument(
        "--nascimento",
        help="data de nascimento do portador (YYYY-MM-DD); obrigatória no DNE, "
        "opcional na FESN (quando informada, confere a titularidade)",
    )
    parser.add_argument("--evento", default="POC meia-entrada", help="nome do evento")
    parser.add_argument("--cnpj", help="CNPJ do promotor/estabelecimento (só o DNE usa)")
    parser.add_argument(
        "--data-evento",
        help="data do evento (YYYY-MM-DD); a validade da carteirinha é conferida contra ela. "
        "Padrão: hoje (America/Sao_Paulo); informá-la vazia é erro",
    )
    parser.add_argument(
        "--provedor",
        choices=("auto", "dne", "fesn"),
        default="auto",
        help="base a consultar; auto tenta todas antes de negar (padrão)",
    )
    parser.add_argument("--demo", action="store_true", help="roda com respostas gravadas, sem rede")
    parser.add_argument("--json", action="store_true", help="saída em JSON")
    return parser


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Everything after the arguments are parsed, output included."""
    if args.demo:
        decisions = {
            name: check(
                "DEMO0001",
                "1999-04-21",
                "Show demo",
                "00.000.000/0001-91",
                DEMO_DATA_EVENTO,
                caller=recorded(payload),
            )
            for name, payload in DEMO_CASES.items()
        }
        if args.json:
            as_dicts = {name: decision.as_dict() for name, decision in decisions.items()}
            print(json.dumps(as_dicts, ensure_ascii=False, indent=2))
        else:
            for name, decision in decisions.items():
                render(decision, name)
        print(
            "\n(demo: nenhuma chamada de rede; use os argumentos reais para validar de verdade)",
            file=sys.stderr if args.json else sys.stdout,
        )
        return 0

    # Só o DNE exige data de nascimento e CNPJ; a FESN consulta pelo código de uso.
    required = ["codigo_uso"] if args.provedor == "fesn" else ["codigo_uso", "nascimento", "cnpj"]
    missing = [f for f in required if not getattr(args, f)]
    if missing:
        parser.error(
            f"faltam argumentos: {', '.join('--' + m.replace('_', '-') for m in missing)}"
            " — ou use --demo"
        )

    if args.provedor == "dne" and not os.environ.get("MEIAENTRADA_CODIGO_ACESSO"):
        print(
            "MEIAENTRADA_CODIGO_ACESSO não está definido. O validador exige um código de "
            "acesso de estabelecimento/produtora — cadastre-se em "
            "https://www.meiaentrada.org.br/validador/integre",
            file=sys.stderr,
        )
        return 2

    if args.provedor == "fesn":
        decision = check_fesn(args.codigo_uso, args.nascimento, data_evento=args.data_evento)
    else:
        runner = check if args.provedor == "dne" else check_any
        decision = runner(
            codigo_uso=args.codigo_uso,
            data_nascimento=args.nascimento,
            nome_evento=args.evento,
            cnpj_promotor=args.cnpj,
            data_evento=args.data_evento,
        )

    if args.json:
        print(json.dumps(decision.as_dict(), ensure_ascii=False, indent=2))
    else:
        render(decision, "resultado")
    if not decision.validated:
        return 3
    return 0 if decision.eligible else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args, parser)
    except InvalidInput as exc:
        print(f"entrada inválida: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — a crash is not a verdict on the card
        print(f"falha inesperada ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
