import re
from peldrun.context.builder import ContextManager
from peldrun.context.generative_ui import build_generative_ui_prompt


def test_generative_ui_disabled_returns_empty():
    manager = ContextManager()
    prompt = manager._format_generative_ui_prompt({"enabled": False})
    assert prompt == ""


def test_generative_ui_empty_allowed_returns_empty():
    prompt = build_generative_ui_prompt({"enabled": True, "allowed_components": []})
    assert prompt == ""


def test_generative_ui_no_literal_escaped_newlines():
    all_components = ["Card", "Table", "Diagram", "Carousel", "Form"]
    prompt = build_generative_ui_prompt({
        "enabled": True,
        "allowed_components": all_components
    })
    # Must not contain literal double-escaped \\n in specs
    assert "\\n" not in prompt


def test_generative_ui_no_placeholder_leaks_in_1shot():
    # Test minimal restricted presets to verify fallback replacement
    restricted_combos = [
        ["Alert"],
        ["Badge"],
        ["Progress"],
        ["Form"],
        ["MathPlot"],
        ["Accordion"],
        ["TestResults"],
    ]
    for combo in restricted_combos:
        prompt = build_generative_ui_prompt({
            "enabled": True,
            "allowed_components": combo
        })
        openui_blocks = re.findall(r"<openui>(.*?)</openui>", prompt, re.DOTALL)
        assert len(openui_blocks) == 1
        block = openui_blocks[0]
        # Must not contain unresolved angle bracket placeholders
        assert not re.search(r"<[A-Za-z0-9_| -]+>", block), f"Placeholder leaked in combo: {combo}\nBlock:\n{block}"


def test_generative_ui_full_catalog_trigger_coverage():
    all_components = [
        "Card", "Metric", "Badge", "Alert", "Progress", "Table",
        "Chart", "Form", "TestResults", "Accordion", "ActionButton",
        "Diagram", "Carousel", "MathPlot"
    ]
    prompt = build_generative_ui_prompt({
        "enabled": True,
        "allowed_components": all_components
    })
    assert "[AUTONOMOUS GENERATIVE UI ENGINE: ACTIVE]" in prompt
    assert "NON-TRIGGER CONDITIONS" in prompt
    assert "BEHAVIORAL FALLBACK RULE" in prompt
    assert "Alert" in prompt
    assert "Accordion" in prompt
    assert "Progress" in prompt
    assert "ActionButton" in prompt
    assert "Chart [<area|bar|line|donut>]" in prompt
