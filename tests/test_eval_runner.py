"""The evaluation runner must refuse to score stub responses as a real evaluation."""
import json

from evals import run_eval


def _run(client, capsys, **kwargs):
    code = run_eval.run(client=client, check_jobs=False, **kwargs)
    return code, json.loads(capsys.readouterr().out)


def test_labelled_mode_refuses_stub_server(client, monkeypatch, capsys):
    monkeypatch.setenv("LLM_STUB", "1")
    code, out = _run(client, capsys, mode="labelled")
    assert code == run_eval.EXIT_WRONG_MODE
    assert out["server_llm_mode"] == "stub"


def test_labelled_mode_refuses_disabled_server(client, monkeypatch, capsys):
    monkeypatch.setenv("LLM_ENABLED", "false")
    code, out = _run(client, capsys, mode="labelled")
    assert code == run_eval.EXIT_WRONG_MODE


def test_contract_mode_passes_against_stub(client, monkeypatch, capsys):
    monkeypatch.setenv("LLM_STUB", "1")
    code, out = _run(client, capsys, mode="contract", require_llm_mode="stub")
    assert code == 0
    assert out["passed"] == out["total"] == 8
    assert out["llm_mode"] == "stub"


def test_contract_mode_fails_on_server_errors(client, monkeypatch, capsys):
    monkeypatch.setenv("LLM_STUB", "0")  # live mode with no provider -> guard raises -> 500s
    code, out = _run(client, capsys, mode="contract")
    assert code == run_eval.EXIT_FAILED
    assert out["failed"] == 8


def test_labelled_mode_scores_live_responses(client, fake_llm, capsys):
    cases = json.loads(run_eval.CASES_PATH.read_text())
    # Answer each case with exactly its expected fields (plus required ones).
    for case in cases:
        answer = {"vendor": None, "date": None, "total_amount": None, "currency": None,
                  "confidence": 0.5, "needs_review": True, **case["expected"]}
        fake_llm.script.append(json.dumps(answer))
    code, out = _run(client, capsys, mode="labelled")
    assert code == 0
    assert out == {"mode": "labelled", "llm_mode": "live", "passed": 8, "total": 8, "failed": 0, "failures": []}
