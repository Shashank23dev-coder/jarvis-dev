"""Shared bounded context assembly for the legacy runner and JarvisBrain."""
from dataclasses import dataclass
import json

from .experience import ExperienceStore, task_type, attempt_capture
from .recall import RecallService, encoded, wants_recall


@dataclass(frozen=True)
class TaskContext:
    goal: str
    task_id: str
    conversation_id: str
    created_at: str

    @classmethod
    def from_task(cls, goal, task):
        return cls(goal, task['id'], task['conversation_id'], task['created_at'])


@dataclass
class AssembledContext:
    history: list[dict]
    recall: dict | None = None
    experiences: dict | None = None
    answer: str | None = None
    stop_reason: str | None = None


def assemble_context(context, store, *, recall_limits=None, experience_limits=None):
    history = [{'role': message['role'], 'content': message['content']}
               for message in store.memory.prompt_history(
                   context.conversation_id, through_task=context.task_id)]
    # Preserve the direct runner's active goal and context-size guard.
    history[-1] = {'role': 'user', 'content': context.goal}
    result = AssembledContext(history)
    if task_type(context.goal) is not None:
        result.experiences = attempt_capture(lambda: ExperienceStore(store.database, limits=experience_limits).search(
            context.goal[:500], before_time=context.created_at, exclude_task_id=context.task_id))
        if result.experiences and result.experiences['results']:
            history.insert(0, {'role': 'user', 'content': encoded(result.experiences)})
    if wants_recall(context.goal):
        task = dict(id=context.task_id, conversation_id=context.conversation_id, created_at=context.created_at)
        result.recall = recall = RecallService(store.database, limits=recall_limits).for_task(context.goal, task)
        if recall.get('mode') == 'conversation_navigation':
            if not recall['results']:
                result.answer = ("I couldn't find readable excerpts from the most recent prior conversation "
                                 "within the bounded history window.")
                result.stop_reason = 'recall_not_found'
                return result
            if recall['brief']:
                excerpts = '; '.join(f"{item['role']}: {json.dumps(item['content'][:100], ensure_ascii=False)}"
                                     for item in recall['results'])
                result.answer = ('The available excerpt from the prior conversation is extremely short: ' + excerpts +
                                 '. There is not enough context to infer a topic or next step.')
                result.stop_reason = 'recall_brief'
                return result
        if not recall['results'] and len(history) == 1:
            result.answer = ("I couldn't find relevant prior conversation history or explicit durable memories "
                             "within the bounded search. Can you remind me of the topic or details?")
            result.stop_reason = 'recall_not_found'
            return result
        history.insert(0, {'role': 'user', 'content': encoded(recall)})
    return result
