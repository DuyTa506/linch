//! [`Timing`]: when a schedule fires, a cron pattern in a time zone or a
//! fixed interval, and the next fire after a given time.

use std::str::FromStr;
use std::time::Duration;

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use croner::Cron;

use crate::error::WorkerError;

/// Fires [`Timing::shortest_gap`] looks at.
const GAP_SAMPLES: usize = 512;

/// When a schedule fires.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Timing {
    /// A cron pattern (five fields, or six with leading seconds, croner
    /// syntax) read in an IANA time zone such as `Europe/Berlin`. A fire
    /// time that falls in a daylight-saving gap moves to the first valid
    /// time after it; one in an overlap fires once, in the first half.
    Cron {
        /// The pattern.
        pattern: String,
        /// The IANA zone name; `UTC` for UTC.
        timezone: String,
    },
    /// Every `Duration` from the schedule's creation (or resume), at least
    /// 1 ms.
    Every(Duration),
}

/// A [`Timing`] that was checked and can compute fires.
pub(crate) enum Parsed {
    Cron(Box<Cron>, Tz),
    Every(chrono::Duration),
}

impl Timing {
    /// A cron pattern in UTC.
    pub fn cron_utc(pattern: impl Into<String>) -> Self {
        Self::Cron {
            pattern: pattern.into(),
            timezone: "UTC".into(),
        }
    }

    pub(crate) fn parse(&self) -> Result<Parsed, WorkerError> {
        match self {
            Self::Cron { pattern, timezone } => {
                let cron = Cron::from_str(pattern).map_err(|source| WorkerError::Cron {
                    pattern: pattern.clone(),
                    source,
                })?;
                let zone = Tz::from_str(timezone)
                    .map_err(|_| WorkerError::Invalid(format!("unknown time zone {timezone:?}")))?;
                Ok(Parsed::Cron(Box::new(cron), zone))
            }
            Self::Every(every) => {
                let too_short = *every < Duration::from_millis(1);
                let every = chrono::Duration::from_std(*every)
                    .ok()
                    .filter(|_| !too_short);
                let every = every.ok_or_else(|| {
                    WorkerError::Invalid("the interval must be from 1 ms to 292 years".into())
                })?;
                Ok(Parsed::Every(every))
            }
        }
    }

    /// Check the pattern and zone, or the interval.
    ///
    /// Raises:
    ///     WorkerError: If one is invalid.
    pub fn check(&self) -> Result<(), WorkerError> {
        self.parse().map(|_| ())
    }

    /// The first fire strictly after `after`.
    ///
    /// Raises:
    ///     WorkerError: If the pattern, zone or interval is invalid, or the
    ///         pattern never fires after `after`.
    pub fn next_after(&self, after: DateTime<Utc>) -> Result<DateTime<Utc>, WorkerError> {
        self.parse()?.next_after(self, after)
    }

    /// The shortest time between two consecutive fires over the next 512
    /// fires after `from`: the interval itself, or for a cron pattern the
    /// smallest gap it shows over that span (a pattern whose fires bunch
    /// only further out is not caught).
    ///
    /// Raises:
    ///     WorkerError: As [`Self::next_after`].
    pub fn shortest_gap(&self, from: DateTime<Utc>) -> Result<Duration, WorkerError> {
        let parsed = self.parse()?;
        if let Parsed::Every(every) = parsed {
            return Ok(every.to_std().unwrap_or(Duration::MAX));
        }
        let mut previous = parsed.next_after(self, from)?;
        let mut shortest = Duration::MAX;
        for _ in 0..GAP_SAMPLES {
            let Ok(next) = parsed.next_after(self, previous) else {
                break;
            };
            let gap = (next - previous).to_std().unwrap_or(Duration::ZERO);
            shortest = shortest.min(gap);
            previous = next;
        }
        Ok(shortest)
    }
}

impl Parsed {
    pub(crate) fn next_after(
        &self,
        timing: &Timing,
        after: DateTime<Utc>,
    ) -> Result<DateTime<Utc>, WorkerError> {
        match self {
            Self::Cron(cron, zone) => {
                let local = after.with_timezone(zone);
                let next = cron.find_next_occurrence(&local, false).map_err(|source| {
                    let pattern = match timing {
                        Timing::Cron { pattern, .. } => pattern.clone(),
                        Timing::Every(_) => String::new(),
                    };
                    WorkerError::Cron { pattern, source }
                })?;
                Ok(next.with_timezone(&Utc))
            }
            Self::Every(every) => after
                .checked_add_signed(*every)
                .ok_or_else(|| WorkerError::Invalid("the next fire is out of range".into())),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(text: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(text).unwrap().to_utc()
    }

    fn cron(pattern: &str, timezone: &str) -> Timing {
        Timing::Cron {
            pattern: pattern.into(),
            timezone: timezone.into(),
        }
    }

    #[test]
    fn the_next_fire_is_strictly_after() {
        let hourly = Timing::cron_utc("0 * * * *");
        let next = hourly.next_after(at("2026-11-06T10:15:00Z")).unwrap();
        assert_eq!(next, at("2026-11-06T11:00:00Z"));
        // A fire exactly on a tick moves to the following tick.
        let next = hourly.next_after(at("2026-11-06T11:00:00Z")).unwrap();
        assert_eq!(next, at("2026-11-06T12:00:00Z"));
    }

    #[test]
    fn a_seconds_field_is_accepted() {
        let every_ten = Timing::cron_utc("*/10 * * * * *");
        let next = every_ten.next_after(at("2026-11-06T10:15:01Z")).unwrap();
        assert_eq!(next, at("2026-11-06T10:15:10Z"));
    }

    #[test]
    fn a_bad_pattern_or_zone_names_itself() {
        let error = Timing::cron_utc("61 * * * *").parse().err().unwrap();
        assert!(matches!(&error, WorkerError::Cron { pattern, .. } if pattern == "61 * * * *"));
        assert!(Timing::cron_utc("not a cron").parse().is_err());
        let error = cron("0 9 * * *", "Mars/Olympus").parse().err().unwrap();
        assert!(error.to_string().contains("Mars/Olympus"), "{error}");
        assert!(Timing::Every(Duration::ZERO).parse().is_err());
        assert!(Timing::Every(Duration::from_micros(10)).parse().is_err());
    }

    #[test]
    fn a_cron_pattern_reads_in_its_time_zone() {
        // 09:00 in New York is 14:00 UTC in winter (EST) and 13:00 in
        // summer (EDT).
        let nine = cron("0 9 * * *", "America/New_York");
        let winter = nine.next_after(at("2026-01-10T00:00:00Z")).unwrap();
        assert_eq!(winter, at("2026-01-10T14:00:00Z"));
        let summer = nine.next_after(at("2026-07-10T00:00:00Z")).unwrap();
        assert_eq!(summer, at("2026-07-10T13:00:00Z"));
    }

    #[test]
    fn a_fire_in_the_spring_gap_moves_after_it() {
        // 2026-03-08 in New York: 02:00 EST jumps to 03:00 EDT, so 02:30
        // does not exist that day; it fires at 03:00 EDT (07:00 UTC).
        let half_past_two = cron("30 2 * * *", "America/New_York");
        let next = half_past_two
            .next_after(at("2026-03-08T05:00:00Z"))
            .unwrap();
        assert_eq!(next, at("2026-03-08T07:00:00Z"));
        // The next day it is 02:30 EDT again.
        let after = half_past_two.next_after(next).unwrap();
        assert_eq!(after, at("2026-03-09T06:30:00Z"));
    }

    #[test]
    fn a_fire_in_the_autumn_overlap_happens_once() {
        // 2026-11-01 in New York: 01:00-02:00 happens twice (EDT, then
        // EST). 01:30 fires once, in the first (EDT) half.
        let half_past_one = cron("30 1 * * *", "America/New_York");
        let first = half_past_one
            .next_after(at("2026-11-01T04:00:00Z"))
            .unwrap();
        assert_eq!(first, at("2026-11-01T05:30:00Z"));
        let next = half_past_one.next_after(first).unwrap();
        assert_eq!(next, at("2026-11-02T06:30:00Z"));
    }

    #[test]
    fn an_interval_adds_to_the_last_fire() {
        let every = Timing::Every(Duration::from_secs(90));
        let next = every.next_after(at("2026-11-06T10:00:00Z")).unwrap();
        assert_eq!(next, at("2026-11-06T10:01:30Z"));
    }

    #[test]
    fn the_shortest_gap_binds_cron_and_intervals() {
        let from = at("2026-11-06T10:00:00Z");
        let seconds = Timing::cron_utc("* * * * * *");
        assert_eq!(seconds.shortest_gap(from).unwrap(), Duration::from_secs(1));
        let hourly = Timing::cron_utc("0 * * * *");
        assert_eq!(
            hourly.shortest_gap(from).unwrap(),
            Duration::from_secs(3600)
        );
        // Two fires a minute apart each hour.
        let pair = Timing::cron_utc("0,1 * * * *");
        assert_eq!(pair.shortest_gap(from).unwrap(), Duration::from_secs(60));
        let every = Timing::Every(Duration::from_secs(5));
        assert_eq!(every.shortest_gap(from).unwrap(), Duration::from_secs(5));
    }
}
