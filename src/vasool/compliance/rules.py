"""One pure function per compliance rule. No I/O, no clock reads — everything is in `context`.

Each rule is wrapped in a `Rule` so its `rule_id` travels with the function instead of being
a string literal duplicated at every call site.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from vasool.compliance.buckets import TokenBucket
from vasool.compliance.constants import (
    AFA_ELEVATED_CATEGORIES,
    AFA_FREE_ELEVATED_LIMIT,
    AFA_FREE_LIMIT,
    CONTACT_QUIET_HOURS_IST,
    MAX_ATTEMPT_WINDOW_DAYS,
    MAX_MESSAGES_PER_INVOICE,
    MAX_SILENT_ATTEMPTS,
    MIN_HOURS_BETWEEN_MESSAGES,
    MIN_HOURS_BETWEEN_SAME_PATH,
    PRE_DEBIT_NOTICE_HOURS,
)
from vasool.domain.money import Money
from vasool.domain.taxonomy import HARD_DECLINE_CLASSES
from vasool.domain.timezones import to_ist
from vasool.domain.types import (
    ActionType,
    Attempt,
    CustomerProfile,
    FailureClass,
    Invoice,
    MandateState,
    RuleResult,
)

# Action types that put a message in front of the customer — quiet hours, frequency caps
# and content requirements apply to these and not to a silent debit.
MESSAGE_ACTION_TYPES: frozenset[ActionType] = frozenset(
    {ActionType.PRE_DEBIT_NOTICE, ActionType.CONTACT_LINK, ActionType.CREDENTIAL_UPDATE_REQUEST}
)

# A hard decline forbids both the debit itself and the notice that would precede it.
NO_RETRY_ACTION_TYPES: frozenset[ActionType] = frozenset(
    {ActionType.SILENT_RETRY, ActionType.PRE_DEBIT_NOTICE}
)


@dataclass(frozen=True)
class OutboundMessage:
    """The content of a customer-facing message, for R014 to check against canonical values."""

    merchant_name: str | None
    amount: Money | None
    debit_date: date | None
    opt_out_included: bool


@dataclass(frozen=True)
class RuleContext:
    """Everything a rule needs, passed in. Assembled by the caller, never read live by a rule."""

    invoice: Invoice
    customer: CustomerProfile
    failure_class: FailureClass
    now: datetime
    prior_attempts: tuple[Attempt, ...] = ()
    notice_delivered_at: datetime | None = None
    issuer_down: bool = False
    issuer_bucket: TokenBucket | None = None
    message: OutboundMessage | None = None


def _scheduled_at(attempt: Attempt) -> datetime | None:
    return attempt.debit_at or attempt.notify_at


@dataclass(frozen=True)
class Rule:
    rule_id: str
    evaluate: Callable[[Attempt, RuleContext], RuleResult]

    def __call__(self, attempt: Attempt, context: RuleContext) -> RuleResult:
        return self.evaluate(attempt, context)


def _r001(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R001_PRE_DEBIT_NOTICE"
    if attempt.debit_at is None:
        return RuleResult(rule_id=rule_id, passed=True)
    if context.notice_delivered_at is None:
        return RuleResult(rule_id=rule_id, passed=False, reason="no pre-debit notice delivered")
    lead_time = attempt.debit_at - context.notice_delivered_at
    if lead_time < timedelta(hours=PRE_DEBIT_NOTICE_HOURS):
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=f"only {lead_time} notice, need >= {PRE_DEBIT_NOTICE_HOURS}h",
        )
    return RuleResult(rule_id=rule_id, passed=True)


R001_PRE_DEBIT_NOTICE = Rule("R001_PRE_DEBIT_NOTICE", _r001)


def _r002(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R002_AFA_THRESHOLD"
    limit = (
        AFA_FREE_ELEVATED_LIMIT
        if context.invoice.category in AFA_ELEVATED_CATEGORIES
        else AFA_FREE_LIMIT
    )
    if attempt.amount.paise <= limit.paise:
        return RuleResult(rule_id=rule_id, passed=True)
    if attempt.action_type is ActionType.SILENT_RETRY:
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=(
                f"{attempt.amount.format_inr()} exceeds AFA-free limit "
                f"{limit.format_inr()}; needs AFA, cannot be silent"
            ),
        )
    return RuleResult(rule_id=rule_id, passed=True)


R002_AFA_THRESHOLD = Rule("R002_AFA_THRESHOLD", _r002)


def _r003(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R003_MAX_SILENT_ATTEMPTS"
    if attempt.action_type is not ActionType.SILENT_RETRY:
        return RuleResult(rule_id=rule_id, passed=True)
    prior_silent = sum(
        1 for a in context.prior_attempts if a.action_type is ActionType.SILENT_RETRY
    )
    if prior_silent + 1 > MAX_SILENT_ATTEMPTS:
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=f"would be silent attempt {prior_silent + 1}, max is {MAX_SILENT_ATTEMPTS}",
        )
    return RuleResult(rule_id=rule_id, passed=True)


R003_MAX_SILENT_ATTEMPTS = Rule("R003_MAX_SILENT_ATTEMPTS", _r003)


def _r004(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R004_ATTEMPT_WINDOW"
    scheduled_at = _scheduled_at(attempt)
    if scheduled_at is None:
        return RuleResult(rule_id=rule_id, passed=True)
    elapsed = scheduled_at - context.invoice.first_failed_at
    if elapsed > timedelta(days=MAX_ATTEMPT_WINDOW_DAYS):
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=f"{elapsed} after first failure, window is {MAX_ATTEMPT_WINDOW_DAYS}d",
        )
    return RuleResult(rule_id=rule_id, passed=True)


R004_ATTEMPT_WINDOW = Rule("R004_ATTEMPT_WINDOW", _r004)


def _r005(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R005_MIN_INTERVAL_SAME_PATH"
    scheduled_at = _scheduled_at(attempt)
    if scheduled_at is None:
        return RuleResult(rule_id=rule_id, passed=True)
    same_path_times = [
        _scheduled_at(a) for a in context.prior_attempts if a.rail == attempt.rail
    ]
    prior_times = [t for t in same_path_times if t is not None]
    if not prior_times:
        return RuleResult(rule_id=rule_id, passed=True)
    gap = scheduled_at - max(prior_times)
    if gap < timedelta(hours=MIN_HOURS_BETWEEN_SAME_PATH):
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=(
                f"only {gap} since last {attempt.rail.value} attempt, "
                f"need >= {MIN_HOURS_BETWEEN_SAME_PATH}h"
            ),
        )
    return RuleResult(rule_id=rule_id, passed=True)


R005_MIN_INTERVAL_SAME_PATH = Rule("R005_MIN_INTERVAL_SAME_PATH", _r005)


def _r006(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R006_HARD_DECLINE_NO_RETRY"
    if context.failure_class not in HARD_DECLINE_CLASSES:
        return RuleResult(rule_id=rule_id, passed=True)
    if attempt.action_type in NO_RETRY_ACTION_TYPES:
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=f"{context.failure_class.value} is a hard decline, no retry or notice allowed",
        )
    return RuleResult(rule_id=rule_id, passed=True)


R006_HARD_DECLINE_NO_RETRY = Rule("R006_HARD_DECLINE_NO_RETRY", _r006)


def _r007(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R007_MANDATE_ACTIVE"
    if attempt.debit_at is None:
        return RuleResult(rule_id=rule_id, passed=True)
    if context.customer.mandate_state is not MandateState.ACTIVE:
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=f"mandate is {context.customer.mandate_state.value}, not active",
        )
    return RuleResult(rule_id=rule_id, passed=True)


R007_MANDATE_ACTIVE = Rule("R007_MANDATE_ACTIVE", _r007)


def _r008(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R008_MANDATE_CAP"
    if attempt.debit_at is None:
        return RuleResult(rule_id=rule_id, passed=True)
    if attempt.amount.paise > context.customer.mandate_max_amount.paise:
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=(
                f"{attempt.amount.format_inr()} exceeds mandate cap "
                f"{context.customer.mandate_max_amount.format_inr()}"
            ),
        )
    return RuleResult(rule_id=rule_id, passed=True)


R008_MANDATE_CAP = Rule("R008_MANDATE_CAP", _r008)


def _r009(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R009_CONTACT_QUIET_HOURS"
    if attempt.action_type not in MESSAGE_ACTION_TYPES:
        return RuleResult(rule_id=rule_id, passed=True)
    send_at = _scheduled_at(attempt)
    if send_at is None:
        return RuleResult(rule_id=rule_id, passed=True)
    hour = to_ist(send_at).hour
    quiet_start, quiet_end = CONTACT_QUIET_HOURS_IST
    in_quiet = hour >= quiet_start or hour < quiet_end
    if in_quiet:
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=f"{hour:02d}:00 IST is inside quiet hours {quiet_start}:00-{quiet_end}:00",
        )
    return RuleResult(rule_id=rule_id, passed=True)


R009_CONTACT_QUIET_HOURS = Rule("R009_CONTACT_QUIET_HOURS", _r009)


def _r010(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R010_MESSAGE_FREQUENCY"
    if attempt.action_type not in MESSAGE_ACTION_TYPES:
        return RuleResult(rule_id=rule_id, passed=True)
    prior_messages = [a for a in context.prior_attempts if a.action_type in MESSAGE_ACTION_TYPES]
    if len(prior_messages) + 1 > MAX_MESSAGES_PER_INVOICE:
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=f"would be message {len(prior_messages) + 1}, max is {MAX_MESSAGES_PER_INVOICE}",
        )
    send_at = _scheduled_at(attempt)
    prior_times = [t for t in (_scheduled_at(a) for a in prior_messages) if t is not None]
    if send_at is not None and prior_times:
        gap = send_at - max(prior_times)
        if gap < timedelta(hours=MIN_HOURS_BETWEEN_MESSAGES):
            return RuleResult(
                rule_id=rule_id,
                passed=False,
                reason=f"only {gap} since last message, need >= {MIN_HOURS_BETWEEN_MESSAGES}h",
            )
    return RuleResult(rule_id=rule_id, passed=True)


R010_MESSAGE_FREQUENCY = Rule("R010_MESSAGE_FREQUENCY", _r010)


def _r011(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R011_ISSUER_RATE_LIMIT"
    if attempt.debit_at is None or context.issuer_bucket is None:
        return RuleResult(rule_id=rule_id, passed=True)
    if context.issuer_bucket.tokens_at(attempt.debit_at) < 1:
        return RuleResult(
            rule_id=rule_id, passed=False, reason=f"issuer token bucket exhausted at {attempt.debit_at}"
        )
    return RuleResult(rule_id=rule_id, passed=True)


R011_ISSUER_RATE_LIMIT = Rule("R011_ISSUER_RATE_LIMIT", _r011)


def _r012(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R012_CUSTOMER_OPT_OUT"
    if context.customer.opted_out and attempt.action_type is not ActionType.STOP:
        return RuleResult(rule_id=rule_id, passed=False, reason="customer opted out")
    return RuleResult(rule_id=rule_id, passed=True)


R012_CUSTOMER_OPT_OUT = Rule("R012_CUSTOMER_OPT_OUT", _r012)


def _r013(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R013_PROMISE_TO_PAY_SUPPRESSION"
    until = context.customer.promise_to_pay_until
    if until is None or attempt.action_type is ActionType.STOP:
        return RuleResult(rule_id=rule_id, passed=True)
    scheduled_at = _scheduled_at(attempt)
    if scheduled_at is None:
        return RuleResult(rule_id=rule_id, passed=True)
    if scheduled_at < until:
        return RuleResult(
            rule_id=rule_id, passed=False, reason=f"suppressed until promise-to-pay date {until}"
        )
    already_used = any(
        (t := _scheduled_at(a)) is not None and t >= until for a in context.prior_attempts
    )
    if already_used:
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason="promise-to-pay already consumed its one follow-up attempt",
        )
    return RuleResult(rule_id=rule_id, passed=True)


R013_PROMISE_TO_PAY_SUPPRESSION = Rule("R013_PROMISE_TO_PAY_SUPPRESSION", _r013)


def _r014(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R014_MESSAGE_CONTENT"
    if attempt.action_type not in MESSAGE_ACTION_TYPES:
        return RuleResult(rule_id=rule_id, passed=True)
    message = context.message
    if message is None:
        return RuleResult(rule_id=rule_id, passed=False, reason="no message content supplied")
    missing = []
    if not message.merchant_name:
        missing.append("merchant name")
    if message.amount is None or message.amount.paise != attempt.amount.paise:
        missing.append("exact amount")
    if message.debit_date is None:
        missing.append("debit date")
    if not message.opt_out_included:
        missing.append("opt-out")
    if missing:
        return RuleResult(rule_id=rule_id, passed=False, reason=f"missing: {', '.join(missing)}")
    return RuleResult(rule_id=rule_id, passed=True)


R014_MESSAGE_CONTENT = Rule("R014_MESSAGE_CONTENT", _r014)


def _r015(attempt: Attempt, context: RuleContext) -> RuleResult:
    rule_id = "R015_ISSUER_DOWNTIME_GATE"
    if attempt.debit_at is None:
        return RuleResult(rule_id=rule_id, passed=True)
    if context.issuer_down:
        return RuleResult(
            rule_id=rule_id,
            passed=False,
            reason=f"issuer down (severity high, unresolved) for {context.customer.issuer}",
        )
    return RuleResult(rule_id=rule_id, passed=True)


R015_ISSUER_DOWNTIME_GATE = Rule("R015_ISSUER_DOWNTIME_GATE", _r015)


ALL_RULES: tuple[Rule, ...] = (
    R001_PRE_DEBIT_NOTICE,
    R002_AFA_THRESHOLD,
    R003_MAX_SILENT_ATTEMPTS,
    R004_ATTEMPT_WINDOW,
    R005_MIN_INTERVAL_SAME_PATH,
    R006_HARD_DECLINE_NO_RETRY,
    R007_MANDATE_ACTIVE,
    R008_MANDATE_CAP,
    R009_CONTACT_QUIET_HOURS,
    R010_MESSAGE_FREQUENCY,
    R011_ISSUER_RATE_LIMIT,
    R012_CUSTOMER_OPT_OUT,
    R013_PROMISE_TO_PAY_SUPPRESSION,
    R014_MESSAGE_CONTENT,
    R015_ISSUER_DOWNTIME_GATE,
)
