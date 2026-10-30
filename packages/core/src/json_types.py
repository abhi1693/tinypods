"""JSON values shared by structured output boundaries."""

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonRecord = dict[str, JsonValue]
