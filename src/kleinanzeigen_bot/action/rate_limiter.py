from dataclasses import dataclass


@dataclass
class CycleBudget:
    maximum: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.maximum < 1:
            raise ValueError("Cycle budget must be positive")

    @property
    def available(self) -> bool:
        return self.used < self.maximum

    def reserve(self) -> None:
        if not self.available:
            raise RuntimeError("Cycle budget exhausted")
        # Count attempts, including ambiguous results, not only successful messages.
        self.used += 1
