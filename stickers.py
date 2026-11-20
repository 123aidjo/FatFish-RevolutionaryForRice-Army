from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Sticker:
    id: str
    emotions: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StickerPolicy:
    enabled: bool = False
    min_interval: float = 60
    repeat_interval: float = 28800
    catalog: tuple[Sticker, ...] = ()

    def candidates(self, text: str, context: str, supported: bool) -> list[str]:
        if not self.enabled or not supported:
            return []
        return [
            sticker.id for sticker in self.catalog
            if (not sticker.keywords or any(word in text for word in sticker.keywords))
            and (not sticker.contexts or any(word in context for word in sticker.contexts))
        ]

    def select(
        self, selection: str | None, emotion: str, intensity: int, candidates: list[str]
    ) -> str | None:
        if selection not in candidates or not intensity:
            return None
        sticker = next(item for item in self.catalog if item.id == selection)
        return selection if not sticker.emotions or emotion in sticker.emotions else None
