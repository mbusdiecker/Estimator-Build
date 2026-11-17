"""Pure-Python RF math for ROMtool.

- FSPL sanity model (free space, dB).
- ITU-R P.1238 indoor path loss model:
      PL = 20*log10(f_MHz) + N*log10(d_m) + Lf*n_floors - 28
  with N per environment (office 30 / commercial 22 / residential 28 by
  default config) and Lf floor penetration loss (dB/floor, config).
- Wall-crossing loss with ITU-R P.2040-style frequency scaling: tabulated loss
  values are referenced at wall_loss_ref_mhz (default 2000 MHz) and scaled
  linearly in the log-frequency domain:
      loss(f) = loss(ref) * log10(f)/log10(ref)
- Composite signal levels by linear (mW-domain) power summation.
- Uplink maximum path loss for a DAS antenna branch (handset -> antenna).
"""

import math


def fspl_db(d_m, f_mhz):
    """Free-space path loss in dB for distance in meters, frequency in MHz.

    Anchors from the SPEC: FSPL(1 m, 2000 MHz) ~= 38.5 dB, FSPL(10 m, 1900 MHz) ~= 58 dB.
    """
    if f_mhz <= 0:
        raise ValueError("frequency must be positive")
    d = float(d_m)
    if d <= 0:
        raise ValueError("distance must be positive")
    return 20.0 * math.log10(d) + 20.0 * math.log10(f_mhz) - 27.55


def itu_p1238_pl_db(f_mhz, d_m, n_exponent, n_floors, floor_loss_db):
    """ITU-R P.1238 indoor path loss (dB).

    n_floors is the number of floor separations between transmitter and the
    prediction point (0 = same floor). Distances below 1 cm are clamped to
    1 cm so log10 stays finite.
    """
    if f_mhz <= 0:
        raise ValueError("frequency must be positive")
    d = max(float(d_m), 0.01)
    return (
        20.0 * math.log10(f_mhz)
        + n_exponent * math.log10(d)
        + float(floor_loss_db) * n_floors
        - 28.0
    )


def scale_wall_loss_db(loss_ref_db, f_mhz, ref_mhz=2000.0):
    """Scale a tabulated (at ref_mhz) wall penetration loss to f_mhz.

    Linear in the log-frequency domain per the SPEC's P.2040-curve rule:
    the dB value is proportional to log10(f). A material tabulated at 15 dB
    for 2 GHz evaluates to 15*log10(700)/log10(2000) ~= 12.9 dB at 700 MHz,
    which tracks the published P.2040 curves for common materials.
    """
    if f_mhz <= 0:
        raise ValueError("frequency must be positive")
    loss_ref_db = float(loss_ref_db)
    if loss_ref_db == 0.0:
        return 0.0
    return loss_ref_db * math.log10(f_mhz) / math.log10(float(ref_mhz))


def dbm_to_mw(dbm):
    return 10.0 ** (float(dbm) / 10.0)


def mw_to_dbm(mw):
    return 10.0 * math.log10(float(mw))


def composite_dbm(dbm_values):
    """Composite level of co-channel contributions: linear (mW) summation.

    Returns None if there are no contributions.
    """
    vals = [float(v) for v in dbm_values if v is not None]
    if not vals:
        return None
    return mw_to_dbm(sum(dbm_to_mw(v) for v in vals))


def uplink_max_path_loss_db(params, antenna_gain_dbi=0.0, l_dist_db=0.0):
    """Uplink maximum path loss (dB) for a DAS antenna branch.

    params carries the config `uplink` values: handset_tx_dbm, edge_rbs,
    rb_bw_hz, ul_sinr_db, system_noise_figure_db, margin_db. A cell-edge
    handset puts handset_tx_dbm - 10*log10(edge_rbs) on the resource blocks it
    transmits on; the antenna must receive one resource block above the noise
    floor -174 dBm/Hz + 10*log10(rb_bw_hz) + system_noise_figure_db plus
    ul_sinr_db, less margin_db, through the same passive distribution loss
    (L_dist) the downlink sees:

        UL max PL = handset_tx_dbm - 10*log10(edge_rbs) + antenna_gain_dbi
                    - L_dist - (-174 + 10*log10(rb_bw_hz)
                                + system_noise_figure_db + ul_sinr_db)
                    - margin_db
    """
    handset = float(params.get("handset_tx_dbm", 23.0))
    rbs = float(params.get("edge_rbs", 6.0))
    rb_bw = float(params.get("rb_bw_hz", 180000.0))
    sinr = float(params.get("ul_sinr_db", 0.0))
    nf = float(params.get("system_noise_figure_db", 20.0))
    margin = float(params.get("margin_db", 0.0))
    if handset < 0:
        raise ValueError("uplink.handset_tx_dbm must be >= 0")
    if rbs < 1 or rb_bw <= 0:
        raise ValueError("uplink.edge_rbs must be >= 1 and rb_bw_hz > 0")
    return (handset - 10.0 * math.log10(rbs) + float(antenna_gain_dbi)
            - float(l_dist_db)
            - (-174.0 + 10.0 * math.log10(rb_bw) + nf + sinr)
            - margin)
