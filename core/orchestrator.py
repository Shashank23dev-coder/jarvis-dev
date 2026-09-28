import json
from models.openai_client import ask_model
from core.agent import AgentRunner
from core.brain import JarvisBrain


class Orchestrator:
    def __init__(self, agent_runner: AgentRunner | None = None, *, brain_settings=None, providers=None):
        self.agent_runner = agent_runner or AgentRunner()
        self.brain = JarvisBrain(self.agent_runner, settings=brain_settings, providers=providers)

    def run_goal(self, goal: str, *, task_id: str, task_store):
        return self.brain.run(goal, task_id=task_id, task_store=task_store)

    def create_plan(self, goal: str):
        prompt = f"""
You are the planning brain of an autonomous software engineering agent called Jarvis.

Your job is to turn a user goal into a short engineering plan.

Goal:
{goal}

Return ONLY valid JSON using this structure:

{{
  "goal": "...",
  "steps": [
    {{
      "id": 1,
      "description": "...",
      "agent": "coder|researcher|reviewer|tester"
    }}
  ]
}}

Rules:
- Use 3 to 7 steps.
- Be specific.
- Do not execute anything.
- Do not include markdown.
"""

        raw = ask_model(prompt)

        return json.loads(raw)
