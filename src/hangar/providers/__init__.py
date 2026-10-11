from hangar.providers.base import Capabilities, Provider, ProviderError, TerminalStream

__all__ = ["Capabilities", "Provider", "ProviderError", "TerminalStream", "load_provider"]


def load_provider(name: str, settings) -> Provider:
    if name == "docker":
        from hangar.providers.docker import DockerProvider

        return DockerProvider(settings)
    if name == "kubernetes":
        from hangar.providers.kubernetes import KubernetesProvider

        return KubernetesProvider(settings)
    if name == "e2b":
        from hangar.providers.e2b import E2BProvider

        return E2BProvider(settings)
    raise ValueError(f"unknown provider '{name}' (docker | kubernetes | e2b)")
