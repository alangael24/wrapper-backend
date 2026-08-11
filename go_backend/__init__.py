"""Wrapper backend: gateway OpenAI-compatible sobre OpenCode Go.

Cada usuario del wrapper recibe una suscripcion de OpenCode Go asignada
(una key por usuario). El backend guarda las keys cifradas, proxies las
requests al upstream de Go y registra el uso para vigilar los limites
(5h/$12, semana/$30, mes/$60).
"""

__version__ = "0.1.0"
