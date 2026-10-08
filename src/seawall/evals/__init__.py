"""Evaluation harness: run the agent on tasks with tests and measure what happens.

``seawall eval`` runs coding tasks (a workspace, a prompt, hidden tests) and safety scenarios
(a compromised model trying harmful calls) through the real command-line agent, and
reports pass rate, turns, tokens, cost and time. See docs/evals.md.
"""
