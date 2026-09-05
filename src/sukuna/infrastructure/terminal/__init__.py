from .iterm_backend import Iterm2Backend
from .protocol import TerminalBackend
from .tmux_backend import TmuxBackend

BACKENDS: dict[str, type[TerminalBackend]] = {
    "iterm2": Iterm2Backend,
    "tmux": TmuxBackend,
}
