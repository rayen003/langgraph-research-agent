"""Calculator tool — safe arithmetic evaluation."""

from langchain_core.tools import tool
from simpleeval import simple_eval

from utils.persistence import persist_tool_result


@tool
def calculator(expression: str) -> str:
    """Evaluate a mathematical expression such as '2 + 3 * 4'."""
    try:
        value = str(simple_eval(expression))
        return persist_tool_result(
            "calculator", {"expression": expression},
            value, f"Calculated '{expression}' = {value}",
        )
    except Exception as exc:  # noqa: BLE001
        err = f"Error: {exc}"
        return persist_tool_result(
            "calculator", {"expression": expression},
            err, f"Calculator failed for '{expression}'",
        )
