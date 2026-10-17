"""Installation defaults applied only at agent configuration output boundaries."""
import os

PULSE_HUMAN_EMAILS = 'SHIZUHA_PULSE_HUMAN_EMAILS'


def effective_agent_env(agent_env):
    """Copy desired env; an explicit per-agent value (including empty) wins.

    The installation value is never written into FleetAgent.env. Read it for
    each render so removed/changed installation configuration cannot be cached.
    """
    env = dict(agent_env or {})
    if PULSE_HUMAN_EMAILS not in env and PULSE_HUMAN_EMAILS in os.environ:
        env[PULSE_HUMAN_EMAILS] = os.environ[PULSE_HUMAN_EMAILS]
    return env


def effective_runtime_config(config):
    """Overlay an env edit without adding env to unrelated partial updates."""
    desired = dict(config)
    if 'env' in desired:
        desired['env'] = effective_agent_env(desired['env'])
    return desired


def effective_config_frame(frame):
    """Render a durable lane frame without persisting inherited env in outboxes."""
    from .config_fingerprints import config_fingerprints
    outgoing = dict(frame)
    if isinstance(outgoing.get('config'), dict) and 'env' in outgoing['config']:
        outgoing['config'] = effective_runtime_config(outgoing['config'])
        outgoing['config_fingerprints'] = config_fingerprints(outgoing['config'])
    return outgoing
