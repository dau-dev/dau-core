"""Frozen models that validate at use.

Every dau-core model is frozen, and construction validates it. Two things
get past that. ``model_copy(update=...)`` builds a new instance without
running a single validator, so a copy can carry a value construction would
have refused. And a frozen model can still hold a ``dict``, which anyone
holding the model can change in place.

:class:`CheckedModel` closes both. Its instances re-validate whenever they
are handed back to pydantic (``revalidate_instances="always"``), so a parent
built from a copied child refuses a bad child, and :meth:`check` is that
re-validation on demand: a validated copy, or the error. :meth:`replace` is
the validated form of ``model_copy``. A mapping field annotated
:data:`ReadOnly` is stored as a :class:`FrozenMapping`, which compares,
hashes and serializes as the dict it was built from and refuses every
mutation.

The ccflow result and callable models cannot change base; they take
:class:`CheckedMixin` and the same config entries by hand.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Self, cast

from ccflow import BaseModel
from pydantic import AfterValidator, ConfigDict, PlainSerializer

__all__ = ("CheckedMixin", "CheckedModel", "FrozenMapping", "ReadOnly", "freeze_mapping")


class FrozenMapping(Mapping[Any, Any]):
    """A read-only mapping a frozen model can hold.

    A ``Mapping`` rather than a ``dict`` subclass: a subclass only overrides
    the mutating methods, and ``dict.__setitem__(m, k, v)`` reaches past any
    override, so the design that LOOKS safer is the one with a hole in it.
    Nothing here can write, there is no inherited method that can, and the
    mapping hashes by content so a frozen model holding one stays hashable.

    It reads as the dict it was built from -- equality, membership, iteration
    order, ``dict(...)`` -- and the ``ReadOnly`` annotation carries the
    serializer that puts a plain dict back on the wire, so the canonical
    composition JSON (the design-cache key) is unchanged.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[Any, Any] | Iterable[tuple[Any, Any]] = ()) -> None:
        object.__setattr__(self, "_data", dict(data))

    def __getitem__(self, key: Any) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __or__(self, other: Mapping[Any, Any]) -> FrozenMapping:
        """A NEW mapping with ``other`` merged in; this one is unchanged."""
        return FrozenMapping({**self._data, **dict(other)})

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._data!r})"

    def __hash__(self) -> int:
        return hash(frozenset(self._data.items()))

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self._data,))


def freeze_mapping(value: Mapping[Any, Any]) -> FrozenMapping:
    return value if isinstance(value, FrozenMapping) else FrozenMapping(value)


#: Annotate a ``Mapping`` field with this and it is stored read-only, and
#: serialized as a plain dict:
#: ``config: Annotated[Mapping[str, str], *ReadOnly] = Field(default_factory=FrozenMapping)``.
#: The default must be ``FrozenMapping`` too: pydantic does not validate a
#: default, so ``default_factory=dict`` would leave an unset field holding a
#: plain mutable dict -- and an unhashable one, which a frozen model notices.
ReadOnly = (AfterValidator(freeze_mapping), PlainSerializer(dict, return_type=dict))


class CheckedMixin:
    """``check`` and ``replace`` for a frozen pydantic model."""

    def check(self) -> Self:
        """This model, re-validated: every field constraint and validator
        runs again, which is what ``model_copy`` skipped.

        Validation goes through the field VALUES rather than through the
        instance, because an instance is only revalidated when its own config
        says so, and a polymorphic base cannot say so (see
        :class:`CheckedModel`). Values, not a ``model_dump``: a nested model
        stays a model, so nothing round-trips through JSON to be checked.
        """
        return self.replace()

    def replace(self, **update: Any) -> Self:
        """``model_copy(update=...)`` that validates the result."""
        # the mixin is applied to pydantic models; the cast is what says so to
        # a type checker that only sees the mixin
        cls = cast(type[BaseModel], type(self))
        fields = {name: getattr(self, name) for name in cls.model_fields}
        return cast(Self, cls.model_validate({**fields, **update}))


class CheckedModel(CheckedMixin, BaseModel):
    """The base of every dau-core data model: frozen, and re-validated at
    every boundary it crosses.

    ``revalidate_instances`` is what makes a copied model refuse at the next
    boundary: a parent built from a ``model_copy``-mutated child validates
    the child again and rejects it. It is deliberately NOT on a polymorphic
    base such as :class:`dau_core.generators.CoreGenerator`, because a field
    annotated with the base would revalidate a subclass instance against the
    base and reject the subclass's own fields as extra. Those models get
    :class:`CheckedMixin` and are checked explicitly.
    """

    model_config = ConfigDict(frozen=True, revalidate_instances="always")
