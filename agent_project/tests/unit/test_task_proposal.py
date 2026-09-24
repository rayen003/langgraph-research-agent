from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from file import AgentState, propose_task_node


def proposal_graph():
    graph = StateGraph(AgentState)
    graph.add_node('proposal', propose_task_node)
    graph.add_edge(START, 'proposal')
    graph.add_edge('proposal', END)
    return graph.compile(checkpointer=MemorySaver())


def test_proposal_interrupt_then_confirm_attaches_task():
    app = proposal_graph()
    config = {'configurable': {'thread_id': 'proposal-confirm'}}
    result = app.invoke({'route_decision': {'propose_task': True, 'requested_outputs': ['memo']},
                         'turn_context': {'latest_user_turn': 'Prepare memo tomorrow'}}, config)
    assert result['__interrupt__'][0].value['workflow_id'] == 'tracked_task'
    assert not result.get('task_id')
    task = {'task_id': 'assignment:confirmed', 'goal': 'Prepare memo tomorrow'}
    result = app.invoke(Command(resume={'approved': True, 'task_context': task}), config)
    assert result['task_id'] == task['task_id']
    assert result['turn_context']['active_task'] == task


def test_declining_task_continues_without_task():
    app = proposal_graph()
    config = {'configurable': {'thread_id': 'proposal-decline'}}
    app.invoke({'route_decision': {'propose_task': True}}, config)
    result = app.invoke(Command(resume={'approved': False}), config)
    assert not result.get('__interrupt__')
    assert not result.get('task_id')


def test_ordinary_query_and_existing_task_skip_proposal():
    assert propose_task_node({'route_decision': {'propose_task': False}}) == {}
    assert propose_task_node({'task_id': 'existing', 'route_decision': {'propose_task': True}}) == {}
