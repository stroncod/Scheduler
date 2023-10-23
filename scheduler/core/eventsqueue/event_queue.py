# Copyright (c) 2016-2023 Association of Universities for Research in Astronomy, Inc. (AURA)
# For license information see LICENSE or https://opensource.org/licenses/BSD-3-Clause

from collections import deque
from typing import Deque, FrozenSet, Iterable, Optional

from lucupy.minimodel import NightIndex, Site

from scheduler.services import logger_factory
from .events import Blockage, Event, Interruption, ResumeNight

logger = logger_factory.create_logger(__name__)


class EventQueue:
    def __init__(self, night_indices: FrozenSet[NightIndex], sites: FrozenSet[Site]):
        self._events = {night_idx: {site: deque([]) for site in sites} for night_idx in night_indices}
        self._blockage_stack = []

    def add_event(self, night_idx: NightIndex, site: Site, event: Event) -> None:
        match event:
            case Blockage():
                self._blockage_stack.append(event)
            case Interruption():
                site_deque = self.get_night_events(night_idx, site)
                if site_deque is not None:
                    site_deque.append(event)
                else:
                    raise KeyError(f'Could not add event {event} for night index {night_idx }to site {site.name}.')

    def add_events(self, night_idx: NightIndex, site: Site, events: Iterable[Event]) -> None:
        for event in events:
            self.add_event(night_idx, site, event)

    def check_blockage(self, resume_event: ResumeNight) -> Blockage:
        if self._blockage_stack and len(self._blockage_stack) == 1:
            b = self._blockage_stack.pop()
            b.ends(resume_event.start)
            return b

        raise RuntimeError('Missing blockage for ResumeNight')

    def get_night_events(self, night_idx: NightIndex, site: Site) -> Optional[Deque[Event]]:
        """
        Returns the deque for the site for the night index if it exists, else None.
        """
        night_deques = self._events.get(night_idx)
        if night_deques is None:
            logger.error(f'Tried to access event queue for inactive night index {night_idx}.')
            return None
        site_deque = night_deques.get(site)
        if site_deque is None:
            logger.error(f'Tried to access event queue for night index {night_idx} for inactive site {site.name}.')
        return site_deque