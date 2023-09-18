from .classic import palette_dict as Classic
from .agr_256 import palette_dict as Agr256
from .solarized import palette_dict as Solarized
from .dark_vim import palette_dict as DarkVim
from .midnight import palette_dict as Midnight
from .mono import palette_dict as Mono
from .monokai import palette_dict as Monokai
from .monokai_256 import palette_dict as Monokai256
from .vim import palette_dict as Vim

THEMES = {
    "classic": Classic,
    "vim": Vim,
    "dark vim": DarkVim,
    "midnight": Midnight,
    "monokai": Monokai,
    "solarized": Solarized,
    "mono": Mono,
    "agr-256": Agr256,
    "monokai-256": Monokai256,
}
