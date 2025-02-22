# Copyright (c) 2016-2023 Association of Universities for Research in Astronomy, Inc. (AURA)
# For license information see LICENSE or https://opensource.org/licenses/BSD-3-Clause

from typing import List, Optional
import strawberry # noqa
from astropy.time import Time
from lucupy.minimodel import Site, ALL_SITES, NightIndex

from scheduler.core.service.service import build_service
from scheduler.core.sources import Services, Sources
from scheduler.core.builder.modes import dispatch_with, SchedulerModes
from scheduler.core.eventsqueue import WeatherChange, Fault, EventQueue
from scheduler.db.planmanager import PlanManager


from .types import (SPlans, NewNightPlans, ChangeOriginSuccess,
                    SourceFileHandlerResponse, NewWeatherChange,
                    EventsAddedResponse, EventsAddedSuccess)
from .inputs import AddEventInput, CreateNewScheduleInput, NewFault, UseFilesSourceInput
from .scalars import SOrigin


# TODO: This variables need a Redis cache to work with different mutation correctly
sources = Sources()
event_queue = EventQueue(frozenset([NightIndex(i) for i in range(3)]), ALL_SITES)

# TODO: All times need to be in UTC. This is done here but converted from the Optimizer plans, where it should be done.


@strawberry.type
class Mutation:
    """
    @strawberry.mutation
    def change_mode():
        pass
    """

    @strawberry.mutation
    async def load_sources_files(self, files_input: UseFilesSourceInput) -> SourceFileHandlerResponse:
        service = Services[files_input.service]

        match service:
            case Services.RESOURCE:
                calendar = await files_input.calendar.read()
                gmos_fpu = await files_input.gmos_fpus.read()
                gmos_gratings = await files_input.gmos_gratings.read()

                loaded = sources.use_file(service,
                                          calendar,
                                          gmos_fpu,
                                          gmos_gratings)
                if loaded:
                    return SourceFileHandlerResponse(service=files_input.service,
                                                     loaded=loaded,
                                                     msg=f'Files were loaded for service: {service}')
                else:
                    return SourceFileHandlerResponse(service=files_input.service,
                                                     loaded=loaded,
                                                     msg='Files failed to load!')
            case Services.ENV:
                return SourceFileHandlerResponse(service=files_input.service,
                                                 loaded=False,
                                                 msg='Handler not implemented yet!')
            case Services.CHRONICLE:
                return SourceFileHandlerResponse(service=files_input.service,
                                                 loaded=False,
                                                 msg='Handler not implemented yet!')

    # @strawberry.mutation
    # async def load_sources_form(self):
    # This method loads basic conditions from a form similar to Mercury demo.

    @strawberry.mutation
    def change_origin(self, new_origin: SOrigin, mode: SchedulerModes) -> ChangeOriginSuccess:

        old = str(sources.origin)
        new = str(new_origin)
        if new == 'OCS' and mode is SchedulerModes.SIMULATION:
            raise ValueError('Simulation mode can only work with GPP origin source.')
        elif new == 'GPP' and mode is SchedulerModes.VALIDATION:
            raise ValueError('Validation mode can only work with OCS origin source.')
        if old == str(new_origin):
            return ChangeOriginSuccess(from_origin=old, to_origin=old)
        sources.set_origin(new_origin)
        return ChangeOriginSuccess(from_origin=old, to_origin=str(new_origin))


@strawberry.type
class Query:
    all_plans: List[SPlans] = strawberry.field(resolver=lambda: PlanManager.get_plans())

    @strawberry.field
    def plans(self) -> List[SPlans]:
        return PlanManager.get_plans()

    @strawberry.field
    def site_plans(self, site: Site) -> List[SPlans]:
        return [plans.for_site(site) for plans in PlanManager.get_plans()]

    @strawberry.field
    def schedule(self, new_schedule_input: CreateNewScheduleInput) -> NewNightPlans:
        try:
            builder = dispatch_with(new_schedule_input.mode, sources, event_queue)
            start, end = Time(new_schedule_input.start_time, format='iso', scale='utc'), \
                Time(new_schedule_input.end_time, format='iso', scale='utc')

            scheduler = build_service(start, end,
                                      new_schedule_input.num_nights_to_schedule,
                                      new_schedule_input.site,
                                      builder)
            plans, plans_summary = scheduler()
            splans = [SPlans.from_computed_plans(p, new_schedule_input.site) for p in plans]

        except RuntimeError as e:
            raise RuntimeError(f'Schedule query error: {e}')
        # json_summary = json.dumps(plans_summary)
        return NewNightPlans(night_plans=splans, plans_summary=plans_summary)
