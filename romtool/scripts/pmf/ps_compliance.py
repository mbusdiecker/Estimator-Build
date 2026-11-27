"""PS compliance plates: FCC 47 CFR 90.219 caps, the propagation delay
budget, and the cross-building isolation checklist item. Config estimates
(grade E). No prices, no customer or job names.

ERP per channel: the antenna EIRP from the DL model plus the antenna gain
already radiates per carrier; the code cap is 5 W ERP (37 dBm) per
retransmitted channel.
Noise figure: the code limit is 9 dB for signal boosters (90.219 table);
compare the vendor DS NF.
Uplink noise ERP: -43 dBm in a 10 kHz bandwidth inside the band and -70 dBm
at 1 MHz outside, in the quiescent (squelched) state the amplifier is dormant
so the plate is satisfied by the squelch design; verify at acceptance.
Delay: NTIA TR-11-480 measured a 12.5 kHz channel filter floor of 80 us;
Collier caps the ERCES at 32 us and recommends 25 or 75 kHz Class A filters;
the SBC ERCES Standard drafts 15 us. Fiber adds about 5 us per km; coax
group delay is about 5 ns per metre. This budget reports all three limits.
"""
import argparse
import json
import math

COAX_NS_PER_M = 5.0
FIBER_US_PER_KM = 5.0

VENDOR_NF = {"adrf_fire": 4.5, "westell": 7.0, "comba": 5.0, "adrf_vu": 3.5}
VENDOR_NF_SRC = {"adrf_fire": "FiRe-78 SS (UL NF at max gain)",
                 "westell": "CS45 DS (CS40 figure under 7 dB)",
                 "comba": "V3 manual (under 5 dB at 90 dB)",
                 "adrf_vu": "PSR-VU DS (under 3.5 dB without duplexing)"}

def main():
    ps = argparse.ArgumentParser()
    ps.add_argument("--oem", default="adrf_fire", choices=sorted(VENDOR_NF))
    ps.add_argument("--port", type=float, default=33.0, help="DL port dBm per band")
    ps.add_argument("--ant-gain", type=float, default=5.0, help="service antenna gain dBi")
    ps.add_argument("--filter-khz", type=float, default=12.5, help="Class A channel filter bandwidth")
    ps.add_argument("--fiber-m", type=float, default=60.0, help="total optical path length m")
    ps.add_argument("--coax-m", type=float, default=0.0, help="longest donor coax run m")
    ps.add_argument("--delay-limit-us", type=float, default=15.0,
                    help="AHJ delay cap (SBC 15, Collier 32)")
    ps.add_argument("--cross-building", action="store_true",
                    help="flag that a nearby ERRCS within 300 ft needs the test")
    ps.add_argument("--out")
    a = ps.parse_args()

    erp_dbm = a.port + a.ant_gain
    nf_limit = 9.0
    nf_vendor = VENDOR_NF[a.oem]
    filt_us = 1.0e6 / (a.filter_khz * 1e3)
    delay_us = filt_us + (a.fiber_m / 1000.0) * FIBER_US_PER_KM \
        + a.coax_m * COAX_NS_PER_M / 1000.0
    rec_bw = None
    if delay_us > a.delay_limit_us:
        for cand in (25.0, 75.0, 200.0):
            d = 1.0e6 / (cand * 1e3) + (a.fiber_m / 1000.0) * FIBER_US_PER_KM \
                + a.coax_m * COAX_NS_PER_M / 1000.0
            if d <= a.delay_limit_us:
                rec_bw = cand
                rec_delay = round(d, 1)
                break
    doc = {"basis": "FCC 90.219 and delay compliance plate (grade E); the "
                    "carrier count per band is a job input and not assumed here",
           "oem": a.oem,
           "erp_per_channel": {"dl_port_dbm": a.port, "ant_gain_dbi": a.ant_gain,
                               "erp_dbm": round(erp_dbm, 1),
                               "cap_dbm": 37.0, "cap_w": 5.0,
                               "pass": bool(erp_dbm <= 37.0)},
           "noise_figure": {"vendor_nf_db": nf_vendor, "source": VENDOR_NF_SRC[a.oem],
                            "limit_db": nf_limit,
                            "pass": bool(nf_vendor <= nf_limit)},
           "uplink_noise_erp": {"in_band_limit_dbm_10khz": -43.0,
                                "out_of_band_1mhz_dbm": -70.0,
                                "note": "satisfied by the squelch design in the "
                                        "quiescent state; verify at the no-harm "
                                        "acceptance test (Collier method)"},
           "delay": {"filter_khz": a.filter_khz,
                     "filter_delay_us": round(filt_us, 1),
                     "fiber_us": round(a.fiber_m / 1000.0 * FIBER_US_PER_KM, 2),
                     "coax_us": round(a.coax_m * COAX_NS_PER_M / 1000.0, 2),
                     "total_us": round(delay_us, 1),
                     "limit_us": a.delay_limit_us,
                     "pass": bool(delay_us <= a.delay_limit_us),
                     "filter_wide_enough_khz": rec_bw,
                     "wide_filter_total_us": (round(rec_delay, 1) if rec_bw else None),
                     "note": "NTIA TR-11-480: a 12.5 kHz filter alone is at "
                             "least 80 us; 25/75/200 kHz bring the floor to "
                             "40/13.3/5 us. Collier recommends 25 or 75 kHz."},
           "cross_building_isolation": {
               "required": bool(a.cross_building),
               "rule": "any ERRCS within 300 ft of another building's ERRCS: "
                       "at least 100 dB cross isolation; relocate the donor or "
                       "the antennas or add head-end attenuation (PCTEL "
                       "OPT014-1 procedure; BDA gain 80 dB with a 20 dB "
                       "target validates 100 dB)"},
           "class_b_registration": "Class B boosters must be registered in the "
                                   "FCC signal booster database; record the ID "
                                   "in the equipment list"}
    out = a.out
    if out:
        json.dump(doc, open(out, "w"), indent=1)
    print("ERP/ch %.1f dBm (cap 37) %s | NF %.1f dB (limit 9) %s | delay %.1f us "
          "(limit %.0f) %s%s" % (
              erp_dbm, "PASS" if erp_dbm <= 37.0 else "FAIL",
              nf_vendor, "PASS" if nf_vendor <= 9.0 else "FAIL",
              delay_us, a.delay_limit_us,
              "PASS" if delay_us <= a.delay_limit_us else "FAIL",
              "" if delay_us <= a.delay_limit_us else
              " -> widen the Class A filter to %s kHz (%s us)" % (
                  rec_bw, rec_delay) if rec_bw else " -> exceeds every filter option"))

if __name__ == "__main__":
    main()
