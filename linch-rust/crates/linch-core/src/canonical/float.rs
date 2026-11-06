//! Float formatting that matches Python's `repr`/`json.dumps`, instead of
//! `serde_json`'s own notation thresholds (see the `canonical` module docs).

/// Format `value` exactly as Python's `json.dumps` would.
///
/// Python's `repr` for a float uses the shortest decimal digit string that
/// round-trips, then picks fixed notation when `1e-4 <= |value| < 1e16` and
/// scientific notation (`d[.ddd]e[+-]XX`, sign and at least two exponent
/// digits) otherwise. Rust's `{:e}` formatting of `f64` is built on the same
/// "shortest round-trip decimal" guarantee as its `Display`/`Debug` (all
/// three go through `core::num::flt2dec`), just normalized to one leading
/// digit, so this reuses it for the digits and exponent and rebuilds
/// Python's own notation around them — no extra dependency needed.
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

    let formatted = format!("{value:e}");
    let (negative, rest) = match formatted.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, formatted.as_str()),
    };
    let (mantissa, exp_str) = rest
        .split_once('e')
        .expect("Rust's LowerExp output always has an 'e'");
    let exp: i32 = exp_str
        .parse()
        .expect("LowerExp's exponent is a plain signed integer");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();

    let body = if (-4..=15).contains(&exp) {
        fixed_notation(&digits, exp)
    } else {
        scientific_notation(&digits, exp)
    };
    if negative { format!("-{body}") } else { body }
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

    proptest! {
        #[test]
        fn output_parses_back_to_the_same_bits(value in any::<f64>().prop_filter("finite", |f| f.is_finite())) {
            let text = format_f64(value);
            let parsed: f64 = text.parse().expect("format_f64's output always parses as f64");
            prop_assert_eq!(parsed.to_bits(), value.to_bits());
        }
    }
}
