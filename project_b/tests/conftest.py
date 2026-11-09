"""Synthetic but realistic fixtures so Project B unit tests run without the NYC download."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from project_b.reference import build_reference  # noqa: E402

ZIPS = {"MANHATTAN": [("10001", 40.7506, -73.9972), ("10027", 40.8116, -73.9465)],
        "BROOKLYN": [("11211", 40.7128, -73.9536), ("11201", 40.6943, -73.9903)],
        "QUEENS": [("11368", 40.7498, -73.8605)], "BRONX": [("10472", 40.8297, -73.8692)],
        "STATEN ISLAND": [("10314", 40.6033, -74.1477)]}
TYPES = {"Noise - Residential": (["Loud Music/Party", "Banging/Pounding", "Loud Talking"], "NYPD"),
         "Illegal Parking": (["Blocked Hydrant", "Double Parked Blocking Traffic", "Posted Parking Sign Violation"], "NYPD"),
         "HEAT/HOT WATER": (["ENTIRE BUILDING", "APARTMENT ONLY"], "HPD"),
         "Street Condition": (["Pothole", "Cave-in", "Defective Hardware"], "DOT"),
         "Dirty Condition": (["Trash", "Litter"], "DSNY")}
CLOSED_TEXT = {"NYPD": ["The Police Department responded to the complaint and took action to fix the condition.",
                        "The Police Department responded and upon arrival those responsible for the condition were gone."],
               "HPD": ["The Department of Housing Preservation and Development inspected the following conditions. "
                       "No violations were issued. The complaint has been closed."],
               "DOT": ["The Department of Transportation inspected the condition and repaired the defect."],
               "DSNY": ["The Department of Sanitation investigated this complaint and resolved the condition."]}
OPEN_TEXT = "Your Service Request has been submitted and is being reviewed by the agency."
COLUMNS = ["unique_key", "created_date", "closed_date", "agency", "agency_name", "complaint_type", "descriptor",
           "location_type", "incident_zip", "incident_address", "street_name", "city", "status", "due_date",
           "resolution_description", "resolution_action_updated_date", "community_board", "borough",
           "x_coordinate_state_plane", "y_coordinate_state_plane", "open_data_channel_type", "latitude", "longitude"]


def make_frame(n: int, seed: int, start: str = "2023-03-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    t0 = pd.Timestamp(start)
    for i in range(n):
        borough = list(ZIPS)[rng.integers(len(ZIPS))]
        z, lat, lon = ZIPS[borough][rng.integers(len(ZIPS[borough]))]
        lat += rng.normal(0, 0.004)
        lon += rng.normal(0, 0.004)
        ctype = list(TYPES)[rng.integers(len(TYPES))]
        descs, agency = TYPES[ctype]
        created = t0 + pd.Timedelta(seconds=int(i * 37 + rng.integers(0, 30)))
        is_open = rng.random() < 0.05
        closed = None if is_open else created + pd.Timedelta(hours=float(rng.uniform(0.2, 72)))
        rows.append({
            "unique_key": 50_000_000 + i, "created_date": created.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "closed_date": None if closed is None else closed.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "agency": agency, "agency_name": agency, "complaint_type": ctype, "descriptor": descs[rng.integers(len(descs))],
            "location_type": "Street/Sidewalk", "incident_zip": z,
            "incident_address": f"{rng.integers(1, 999)} {['MAIN', 'BROADWAY', 'ELM', 'OAK'][rng.integers(4)]} STREET",
            "street_name": "MAIN STREET", "city": borough if borough != "MANHATTAN" else "NEW YORK",
            "status": "Open" if is_open else "Closed", "due_date": None,
            "resolution_description": OPEN_TEXT if is_open else CLOSED_TEXT[agency][rng.integers(len(CLOSED_TEXT[agency]))],
            "resolution_action_updated_date": None if closed is None else closed.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "community_board": f"01 {borough}", "borough": borough,
            "x_coordinate_state_plane": float(1_000_000 + (lon + 73.95) * 276_000),
            "y_coordinate_state_plane": float(200_000 + (lat - 40.7) * 364_000),
            "open_data_channel_type": "ONLINE", "latitude": lat, "longitude": lon})
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["unique_key"] = df["unique_key"].astype("Int64")
    for c in df.columns:
        if c not in ("unique_key", "latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"):
            df[c] = df[c].astype("string")
    return df


@pytest.fixture(scope="session")
def ref_frame():
    return make_frame(6000, seed=1)


@pytest.fixture(scope="session")
def reference(ref_frame):
    zmap = {z: b for b, zs in ZIPS.items() for z, _, _ in zs}
    return build_reference(ref_frame, zmap)


@pytest.fixture()
def batch():
    return make_frame(2000, seed=7, start="2023-10-02")
