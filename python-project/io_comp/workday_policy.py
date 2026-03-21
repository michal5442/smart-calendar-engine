"""Strategy Pattern for workday policies.

Each IWorkdayPolicy implementation encapsulates the working hours and
weekend rules for a specific region. Adding a new region means implementing
the interface — no changes to the scheduling algorithm.
"""

from abc import ABC, abstractmethod
from datetime import date, time, datetime, timedelta
from typing import Set

from .models import WorkdaySchedule


class IWorkdayPolicy(ABC):
    """Interface that defines working-day rules for a region."""

    @abstractmethod
    def get_workday_schedule(self) -> WorkdaySchedule:
        """Return the WorkdaySchedule (hours + weekend days) for this region."""
        pass

    @abstractmethod
    def is_working_day(self, target_date: date) -> bool:
        """Return True if *target_date* is a working day."""
        pass

    @abstractmethod
    def get_next_working_day(self, target_date: date) -> date:
        """Return the first working day strictly after *target_date*."""
        pass

    @abstractmethod
    def get_working_hours(self, target_date: date) -> timedelta:
        """Return the total working hours available on *target_date*."""
        pass


class IsraeliWorkdayPolicy(IWorkdayPolicy):
    """Israeli workday: 07:00–19:00, Friday and Saturday off."""

    def get_workday_schedule(self) -> WorkdaySchedule:
        return WorkdaySchedule(start_hour=7, end_hour=19, weekend_days={4, 5})  # Fri=4, Sat=5

    def is_working_day(self, target_date: date) -> bool:
        return not self.get_workday_schedule().is_weekend(target_date)

    def get_next_working_day(self, target_date: date) -> date:
        next_day = target_date + timedelta(days=1)
        while not self.is_working_day(next_day):
            next_day += timedelta(days=1)
        return next_day

    def get_working_hours(self, target_date: date) -> timedelta:
        return timedelta(hours=12) if self.is_working_day(target_date) else timedelta(0)


class USAWorkdayPolicy(IWorkdayPolicy):
    """US workday: 09:00–17:00, Saturday and Sunday off."""

    def get_workday_schedule(self) -> WorkdaySchedule:
        return WorkdaySchedule(start_hour=9, end_hour=17, weekend_days={5, 6})  # Sat=5, Sun=6

    def is_working_day(self, target_date: date) -> bool:
        return not self.get_workday_schedule().is_weekend(target_date)

    def get_next_working_day(self, target_date: date) -> date:
        next_day = target_date + timedelta(days=1)
        while not self.is_working_day(next_day):
            next_day += timedelta(days=1)
        return next_day

    def get_working_hours(self, target_date: date) -> timedelta:
        return timedelta(hours=8) if self.is_working_day(target_date) else timedelta(0)


class EUWorkdayPolicy(IWorkdayPolicy):
    """European workday: 08:00–18:00, Saturday and Sunday off."""

    def get_workday_schedule(self) -> WorkdaySchedule:
        return WorkdaySchedule(start_hour=8, end_hour=18, weekend_days={5, 6})

    def is_working_day(self, target_date: date) -> bool:
        return not self.get_workday_schedule().is_weekend(target_date)

    def get_next_working_day(self, target_date: date) -> date:
        next_day = target_date + timedelta(days=1)
        while not self.is_working_day(next_day):
            next_day += timedelta(days=1)
        return next_day

    def get_working_hours(self, target_date: date) -> timedelta:
        return timedelta(hours=10) if self.is_working_day(target_date) else timedelta(0)


class CustomWorkdayPolicy(IWorkdayPolicy):
    """Fully configurable workday policy for any region or use case."""

    def __init__(self, start_hour: int = 7, end_hour: int = 19, weekend_days: Set[int] = None):
        self.schedule = WorkdaySchedule(
            start_hour=start_hour,
            end_hour=end_hour,
            weekend_days=weekend_days or {5, 6},
        )

    def get_workday_schedule(self) -> WorkdaySchedule:
        return self.schedule

    def is_working_day(self, target_date: date) -> bool:
        return not self.schedule.is_weekend(target_date)

    def get_next_working_day(self, target_date: date) -> date:
        next_day = target_date + timedelta(days=1)
        while not self.is_working_day(next_day):
            next_day += timedelta(days=1)
        return next_day

    def get_working_hours(self, target_date: date) -> timedelta:
        if self.is_working_day(target_date):
            return timedelta(hours=self.schedule.end_hour - self.schedule.start_hour)
        return timedelta(0)


class WorkdayPolicyFactory:
    """Creates IWorkdayPolicy instances by name.

    Register custom policies at runtime with register_policy().
    """

    _policies = {
        "israel": IsraeliWorkdayPolicy,
        "usa": USAWorkdayPolicy,
        "eu": EUWorkdayPolicy,
        "custom": CustomWorkdayPolicy,
    }

    @classmethod
    def create_policy(cls, policy_type: str, **kwargs) -> IWorkdayPolicy:
        """Instantiate a policy by name.

        Args:
            policy_type: One of "israel", "usa", "eu", "custom".
            **kwargs: Passed through to CustomWorkdayPolicy when policy_type="custom".

        Raises:
            ValueError: If *policy_type* is not registered.
        """
        if policy_type not in cls._policies:
            raise ValueError(f"Unknown policy type: {policy_type}")
        policy_class = cls._policies[policy_type]
        return policy_class(**kwargs) if policy_type == "custom" else policy_class()

    @classmethod
    def register_policy(cls, name: str, policy_class: type) -> None:
        """Register a new policy so it can be created by name.

        Raises:
            TypeError: If *policy_class* does not extend IWorkdayPolicy.
        """
        if not issubclass(policy_class, IWorkdayPolicy):
            raise TypeError(f"{policy_class} must extend IWorkdayPolicy")
        cls._policies[name] = policy_class
