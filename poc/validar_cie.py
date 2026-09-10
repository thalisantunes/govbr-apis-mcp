#!/usr/bin/env python3
"""POC: valida meia-entrada de estudante (CIE) na compra ou na portaria.

Uso real (precisa de MEIAENTRADA_CODIGO_ACESSO e de uma CIE de verdade):

    python poc/validar_cie.py --codigo-uso ABC123 --nascimento 1999-04-21 \
        --evento "Show X" --cnpj 00.000.000/0001-91

Sem credencial nem carteirinha à mão, o modo demo percorre os mesmos caminhos de
código com respostas gravadas da API real:

    python poc/validar_cie.py --demo
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from govbr_apis_mcp.cie import InvalidInput, check, check_any, check_fesn, interpret

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


def render(decision, titulo: str) -> None:
    mark = "ELEGIVEL" if decision.eligible else "NAO ELEGIVEL"
    print(f"\n=== {titulo}: {mark} ===")
    print(f"motivo: {decision.reason}")
    if decision.provider:
        print(f"base: {decision.provider}")
    for key, value in decision.holder.items():
        print(f"  {key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codigo-uso", help="código de uso da CIE (o que vem no QR code)")
    parser.add_argument("--nascimento", help="data de nascimento do portador (YYYY-MM-DD)")
    parser.add_argument("--evento", default="POC meia-entrada", help="nome do evento")
    parser.add_argument("--cnpj", help="CNPJ do promotor/estabelecimento")
    parser.add_argument("--data-evento", help="data do evento (YYYY-MM-DD); padrão: hoje")
    parser.add_argument(
        "--provedor",
        choices=("auto", "dne", "fesn"),
        default="auto",
        help="base a consultar; auto tenta todas antes de negar (padrão)",
    )
    parser.add_argument("--demo", action="store_true", help="roda com respostas gravadas, sem rede")
    parser.add_argument("--json", action="store_true", help="saída em JSON")
    args = parser.parse_args()

    if args.demo:
        for name, payload in DEMO_CASES.items():
            decision = interpret(payload)
            if args.json:
                print(json.dumps({name: decision.as_dict()}, ensure_ascii=False, indent=2))
            else:
                render(decision, name)
        print("\n(demo: nenhuma chamada de rede; use os argumentos reais para validar de verdade)")
        return 0

    missing = [f for f in ("codigo_uso", "nascimento", "cnpj") if not getattr(args, f)]
    if missing:
        parser.error(f"faltam argumentos: {', '.join('--' + m.replace('_', '-') for m in missing)}"
                     " — ou use --demo")

    if args.provedor == "dne" and not os.environ.get("MEIAENTRADA_CODIGO_ACESSO"):
        print(
            "MEIAENTRADA_CODIGO_ACESSO não está definido. O validador exige um código de "
            "acesso de estabelecimento/produtora — cadastre-se em "
            "https://www.meiaentrada.org.br/validador/integre",
            file=sys.stderr,
        )
        return 2

    try:
        if args.provedor == "fesn":
            decision = check_fesn(args.codigo_uso, args.nascimento)
        else:
            runner = check if args.provedor == "dne" else check_any
            decision = runner(
                codigo_uso=args.codigo_uso,
                data_nascimento=args.nascimento,
                nome_evento=args.evento,
                cnpj_promotor=args.cnpj,
                data_evento=args.data_evento,
            )
    except InvalidInput as exc:
        print(f"entrada inválida: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(decision.as_dict(), ensure_ascii=False, indent=2))
    else:
        render(decision, "resultado")
    return 0 if decision.eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
