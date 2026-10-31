from agents.base import Agent

_agents: dict[str, Agent] = {}


def register(obj):
    """Class decorator (or call with an instance). Registers one instance of the agent under its id."""
    agent = obj() if isinstance(obj, type) else obj
    if not isinstance(agent, Agent):
        raise TypeError(f"{agent!r} does not implement the Agent protocol (id, name, description, run)")
    if not (isinstance(agent.id, str) and agent.id.replace("_", "").replace("-", "").isalnum()):
        raise ValueError(f"agent id {agent.id!r} must be alphanumeric (with - or _)")
    if agent.id in _agents:
        raise ValueError(f"duplicate agent id {agent.id!r}")
    _agents[agent.id] = agent
    return obj


def get(agent_id: str) -> Agent | None:
    return _agents.get(agent_id)


def all_agents() -> list[Agent]:
    return list(_agents.values())
