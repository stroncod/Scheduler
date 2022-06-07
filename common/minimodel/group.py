from abc import ABC
from dataclasses import dataclass
from datetime import timedelta
from enum import auto, Enum
from typing import FrozenSet, List, Optional, Union

from .constraints import Constraints
from .observation import Observation
from .resource import Resource
from .site import Site

GroupID = str


@dataclass
class Group(ABC):
    """
    This is the base implementation of AND / OR Groups.
    Python does not allow classes to self-reference unless in static contexts,
    so we make a very simple base class to self-reference from subclasses since
    we need this functionality to allow for group nesting.

    * id: the identification of the group
    * group_name: a human-readable name of the group
    * number_to_observe: the number of children in the group that must be observed for the
      group to be considered complete
    * delay_min: used in cadences
    * delay_max: used in cadences
    """
    id: GroupID
    group_name: str
    number_to_observe: int
    delay_min: timedelta
    delay_max: timedelta
    children: Union[List['Group'], Observation]

    def __post_init__(self):
        if self.number_to_observe <= 0:
            msg = f'Group {self.group_name} specifies non-positive {self.number_to_observe} children to be observed.'
            raise ValueError(msg)

    def subgroup_ids(self) -> FrozenSet[GroupID]:
        if isinstance(self.children, Observation):
            return frozenset()
        else:
            return frozenset(subgroup.id for subgroup in self.children)

    def sites(self) -> FrozenSet[Site]:
        if isinstance(self.children, Observation):
            return frozenset([self.children.site])
        else:
            return frozenset.union(*[s.sites() for s in self.children])

    def required_resources(self) -> FrozenSet[Resource]:
        return frozenset(r for c in self.children for r in c.required_resources())

    def wavelengths(self) -> FrozenSet[float]:
        return frozenset(w for c in self.children for w in c.wavelengths())

    def constraints(self) -> FrozenSet[Constraints]:
        return frozenset(cs for c in self.children for cs in c.constraints())

    def observations(self) -> List[Observation]:
        if isinstance(self.children, Observation):
            return [self.children]
        else:
            return [o for g in self.children for o in g.observations()]

    def is_observation_group(self) -> bool:
        return isinstance(self.children, Observation)

    def is_scheduling_group(self) -> bool:
        return not (self.is_observation_group())

    def __len__(self):
        return 1 if isinstance(self.children, Observation) else len(self.children)


class AndOption(Enum):
    """
    Different options available for ordering AND group children.
    CUSTOM is used for cadences.
    """
    CONSEC_ORDERED = auto()
    CONSEC_ANYORDER = auto()
    NIGHT_ORDERED = auto()
    NIGHT_ANYORDER = auto()
    ANYORDER = auto()
    CUSTOM = auto()


@dataclass
class AndGroup(Group):
    """
    The concrete implementation of an AND group.
    It requires an AndOption to specify how its observations should be handled,
    and a previous (which should be an index into the group's children to indicate
    the previously observed child, or None if none of the children have yet been
    observed).
    """
    group_option: AndOption
    previous: Optional[int] = None

    def __post_init__(self):
        super().__post_init__()
        if self.number_to_observe != len(self.children):
            msg = f'AND group {self.group_name} specifies {self.number_to_observe} children to be observed but has ' \
                  f'{len(self.children)} children.'
            raise ValueError(msg)
        if self.previous is not None and (self.previous < 0 or self.previous >= len(self.children)):
            msg = f'AND group {self.group_name} has {len(self.children)} children and an illegal previous value of ' \
                  f'{self.previous}'
            raise ValueError(msg)


@dataclass
class OrGroup(Group):
    """
    The concrete implementation of an OR group.
    The restrictions on an OR group is that it must explicitly require not all
    of its children to be observed.
    """

    def __post_init__(self):
        super().__post_init__()
        if self.number_to_observe >= len(self.children):
            msg = f'OR group {self.group_name} specifies {self.number_to_observe} children to be observed but has ' \
                  f'{len(self.children)} children.'
            raise ValueError(msg)