"""mockpay stands in for another company's system: it must never import Tollgate code."""

import ast
from pathlib import Path

MOCKPAY = Path(__file__).resolve().parents[1] / "src" / "mockpay"


def test_mockpay_never_imports_tollgate() -> None:
    offenders = []
    for path in MOCKPAY.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] == "tollgate"]
    assert offenders == []
