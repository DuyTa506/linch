//! Float formatting that matches Python's `repr`/`json.dumps`, instead of
//! `serde_json`'s own notation thresholds (see the `canonical` module docs).

/// Significant digits of margin requested beyond the longest possible
/// shortest-round-trip length (17, for any finite `f64`). Generous enough
/// that the exact decimal expansion of a double practically never shows an
/// unresolved tie at this depth; see [`format_f64`]'s docs.
const GROUND_TRUTH_DIGITS: usize = 41;

/// Format `value` exactly as Python's `json.dumps` would.
///
/// Python's `repr` for a float is the shortest decimal digit string that
/// round-trips, with exact midpoint ties broken to the even last digit,
/// rendered as fixed notation when `1e-4 <= |value| < 1e16` and scientific
/// notation (`d[.ddd]e[+-]XX`, sign and at least two exponent digits)
/// otherwise.
///
/// This does *not* reuse Rust's own shortest-round-trip formatting
/// (`{:e}`/`Display`/`Debug`, all built on `core::num::flt2dec`): it is
/// shortest and always round-trips, but on a true tie (`value`'s exact
/// binary value sits precisely halfway between two equally-short decimal
/// candidates) it does not promise the same tie-break Python's `repr`
/// does, and empirically disagrees with it in roughly 1 in 5,000 random
/// finite doubles. Instead, this asks Rust's *fixed*-precision formatting
/// for [`GROUND_TRUTH_DIGITS`] significant digits — still correctly
/// rounded, just not shortest — then rounds that long digit string down to
/// each candidate length with an explicit, round-half-to-even
/// implementation, and keeps the shortest length whose rounded value
/// round-trips. That is the same algorithm "shortest, correctly rounded,
/// ties to even" denotes, just computed from a sufficiently precise fixed
/// expansion instead of re-deriving it from `flt2dec`.
///
/// `value` must be finite: canonical JSON has no representation for NaN or
/// infinity ([`crate::json::JsonValue`]'s `Number` cannot hold either).
pub(super) fn format_f64(value: f64) -> String {
    debug_assert!(value.is_finite(), "canonical JSON numbers are finite");

    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0".to_string()
        } else {
            "0.0".to_string()
        };
    }

    let (digits, exp) = shortest_round_trip_digits(value.abs());
    let digits: String = digits.iter().map(|d| (d + b'0') as char).collect();

    let body = if (-4..=15).contains(&exp) {
        fixed_notation(&digits, exp)
    } else {
        scientific_notation(&digits, exp)
    };
    if value.is_sign_negative() {
        format!("-{body}")
    } else {
        body
    }
}

/// The shortest significant-digit string (most significant digit first)
/// and decimal exponent such that `0.<digits> * 10^(exp + 1)` parses back
/// to `magnitude` exactly, breaking an exact tie toward the even last
/// digit. `magnitude` must be finite and positive.
fn shortest_round_trip_digits(magnitude: f64) -> (Vec<u8>, i32) {
    let target_bits = magnitude.to_bits();
    let round_trips = |digits: &[u8], exp: i32| -> bool {
        let text: String = digits.iter().map(|d| (d + b'0') as char).collect();
        scientific_notation(&text, exp)
            .parse::<f64>()
            .is_ok_and(|v| v.to_bits() == target_bits)
    };

    let (ground_truth, exp) = long_correctly_rounded_digits(magnitude);
    for len in 1..=17 {
        let (candidate, exp_delta) = round_to_length(&ground_truth, len);
        let candidate_exp = exp + exp_delta;
        if round_trips(&candidate, candidate_exp) {
            return (candidate, candidate_exp);
        }
    }
    unreachable!("17 significant digits always round-trip a finite f64");
}

/// `magnitude`'s digits (most significant first) and decimal exponent, to
/// [`GROUND_TRUTH_DIGITS`] significant digits, via Rust's fixed-precision
/// float formatting (correctly rounded, unlike its *shortest* formatting's
/// tie-break — see [`format_f64`]'s docs). `magnitude` must be finite and
/// positive.
fn long_correctly_rounded_digits(magnitude: f64) -> (Vec<u8>, i32) {
    let formatted = format!("{:.*e}", GROUND_TRUTH_DIGITS - 1, magnitude);
    let (mantissa, exp_str) = formatted
        .split_once('e')
        .expect("Rust's fixed-precision exponential form always has an 'e'");
    let exp: i32 = exp_str
        .parse()
        .expect("the exponent is a plain signed integer");
    let digits: Vec<u8> = mantissa
        .bytes()
        .filter(|b| *b != b'.')
        .map(|b| b - b'0')
        .collect();
    (digits, exp)
}

/// Rounds `digits` (assumed correctly rounded already, i.e. a sufficiently
/// precise ground truth) down to `len` significant digits using
/// round-half-to-even, returning the rounded digit string (exactly `len`
/// digits) and the exponent delta — `1` if rounding up carried out of the
/// leading digit (`"999..." -> "100..."`), else `0`.
fn round_to_length(digits: &[u8], len: usize) -> (Vec<u8>, i32) {
    let (head, tail) = digits.split_at(len);
    let round_up = match tail[0].cmp(&5) {
        std::cmp::Ordering::Less => false,
        std::cmp::Ordering::Greater => true,
        std::cmp::Ordering::Equal if tail[1..].iter().any(|&d| d != 0) => true,
        // Exactly "5" then all zeros within our precision margin: a true
        // tie. Round to even, matching Python.
        std::cmp::Ordering::Equal => head[len - 1] % 2 == 1,
    };
    if round_up {
        increment(head)
    } else {
        (head.to_vec(), 0)
    }
}

/// `digits` plus one decimal unit in its last place, as a same-length (or,
/// on a carry out of the leading digit, renormalized) digit string, paired
/// with the exponent delta that renormalization requires (`"999" -> "100"`
/// at `+ 1`, else `0`).
fn increment(digits: &[u8]) -> (Vec<u8>, i32) {
    let mut out = digits.to_vec();
    for i in (0..out.len()).rev() {
        if out[i] == 9 {
            out[i] = 0;
        } else {
            out[i] += 1;
            return (out, 0);
        }
    }
    // Every digit was 9: "999" -> "1000", renormalized to "100" at exp + 1.
    let mut carried = vec![1u8];
    carried.extend(std::iter::repeat_n(0u8, digits.len() - 1));
    (carried, 1)
}

/// Renders `digits` (the significant digits, most significant first) with a
/// decimal point placed so the value equals `0.<digits> * 10^(exp + 1)`,
/// always keeping at least one digit on each side of the point.
fn fixed_notation(digits: &str, exp: i32) -> String {
    let point = exp + 1; // how many digits belong before the decimal point
    if point <= 0 {
        format!("0.{}{digits}", "0".repeat((-point) as usize))
    } else if point as usize >= digits.len() {
        format!("{digits}{}.0", "0".repeat(point as usize - digits.len()))
    } else {
        let (head, tail) = digits.split_at(point as usize);
        format!("{head}.{tail}")
    }
}

/// Renders `digits` as `d[.ddd]e[+-]XX`: one leading digit, the rest after a
/// decimal point if there are more, a signed exponent padded to at least
/// two digits.
fn scientific_notation(digits: &str, exp: i32) -> String {
    let mantissa = if digits.len() == 1 {
        digits.to_string()
    } else {
        format!("{}.{}", &digits[..1], &digits[1..])
    };
    let sign = if exp >= 0 { '+' } else { '-' };
    format!("{mantissa}e{sign}{:02}", exp.abs())
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::format_f64;

    #[test]
    fn matches_python_json_dumps_pinned_vectors() {
        // Generated with:
        // `.venv/bin/python -I -c 'import json; print(json.dumps(<value>))'`
        let cases: &[(f64, &str)] = &[
            (1e-5, "1e-05"),
            (1e-6, "1e-06"),
            (1e-7, "1e-07"),
            (2.5e-6, "2.5e-06"),
            (9.99e-5, "9.99e-05"),
            (1e-4, "0.0001"),
            (1.234e-4, "0.0001234"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1e22, "1e+22"),
            (1.2345678901234567e19, "1.2345678901234567e+19"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (100.0, "100.0"),
            (0.1, "0.1"),
        ];
        for (value, expected) in cases {
            assert_eq!(format_f64(*value), *expected, "value = {value:e}");
        }
    }

    #[test]
    fn boundary_exponents_switch_notation_exactly_where_python_does() {
        // 9999999999999998.0 is the largest f64 below 1e16: still fixed.
        assert_eq!(format_f64(9999999999999998.0), "9999999999999998.0");
        // 1e16 itself, and anything from there up, is scientific.
        assert_eq!(format_f64(1.0000000000000002e16), "1.0000000000000002e+16");
    }

    #[test]
    fn exact_midpoint_ties_round_to_the_even_last_digit() {
        // Found by sampling 50,000+ random finite f64 bit patterns and
        // diffing `format_f64` against `.venv/bin/python -I -c 'import
        // json; print(json.dumps(...))'`: Rust's shortest-round-trip `{:e}`
        // picked the odd neighbor in each case. Every one of these values'
        // exact binary expansion (checked with Python's
        // `decimal.Decimal(value)`) sits precisely halfway between the
        // pinned digit string and a same-length neighbor one unit away in
        // the last place.
        let cases: &[(u64, &str)] = &[
            (4831287189976473874, "991498632834082.2"),
            (4835102183266106517, "1810845681233701.2"),
            (4834829086699057685, "1742571539471493.2"),
            (4836386614076018701, "2131953383711747.2"),
            (4820401103180786644, "189159422553950.62"),
            (4832521252328027825, "1165612946714028.2"),
            (4834582077876826849, "1680819333913784.2"),
            (14059008248635943617, "-1944352809999024.2"),
            (4835706815585704313, "1962003761133150.2"),
            (4835932167274691569, "2018341683379964.2"),
            (4830526489720817842, "896411100877078.2"),
        ];
        for (bits, expected) in cases {
            assert_eq!(format_f64(f64::from_bits(*bits)), *expected);
        }
    }

    #[test]
    fn a_wide_round_trip_window_does_not_misfire_the_tie_break() {
        // 1.2345678901234567e19's exact value continues "...567168...", not
        // "...567500...": its *shortest* candidate's neighbor
        // ("...568e19") also happens to round-trip (the window at this
        // magnitude is wider than one unit in the 17th digit), but this is
        // not a tie, and the correct, nearer digit ("...567") must win.
        assert_eq!(format_f64(1.2345678901234567e19), "1.2345678901234567e+19");
    }

    proptest! {
        #[test]
        fn output_parses_back_to_the_same_bits(value in any::<f64>().prop_filter("finite", |f| f.is_finite())) {
            let text = format_f64(value);
            let parsed: f64 = text.parse().expect("format_f64's output always parses as f64");
            prop_assert_eq!(parsed.to_bits(), value.to_bits());
        }
    }
}
