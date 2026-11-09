"""Family B (ML part) — learned models, used only where a model adds signal a lookup table cannot.

* lat_lon_swap      — linear regression lat/lon ~ (x, y) state-plane coordinates (two independent
                      location encodings in the same row); a large residual means the lat/lon pair is
                      inconsistent with the row's own x/y. Rows without x/y fall back to a robust z-score.
* near_duplicates   — blocking on (complaint type, ZIP) + rapidfuzz token similarity of address and
                      descriptor + time proximity (fuzzy matching, the standard entity-resolution recipe).
* zip_borough       — geographic consistency: distance from the row's coordinates to the stated ZIP's
                      reference centroid, in units of that ZIP's spread (kNN-style geometric check).
* complaint/descr.  — TF-IDF char n-grams + multinomial logistic regression predicting complaint type from
                      the descriptor; flag when P(stated type) is tiny. Generalises to unseen spellings.
* resolution/status — TF-IDF word n-grams + logistic regression predicting closed vs not-closed from the
                      resolution text; flag when the stated status is improbable.
* pii               — libphonenumber matcher + out-of-vocabulary capitalised bigrams (statistical novelty
                      against the agency-template vocabulary).

Alternatives measured side by side in the benchmark ("alternatives" table in RESULTS_B): an Isolation
Forest on (lat, lon, x, y) for the coordinate problem. The regression is preferred when its measured
precision/recall are at least as good, because its alert can say *why* (the row disagrees with its own x/y).
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
from rapidfuzz import fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LinearRegression, LogisticRegression

from project_b.detection_types import Alert, Detector
from project_b.pii_detector import phonenumbers_pii
from project_b.reference import Reference, parse_dates
from project_b.schema_detector import _MAX_ALERTS


class CoordinateRegression(Detector):
    name, family, problem, algorithm = "xy_latlon_regression", "ml", "lat_lon_swap", "LinearRegression lat,lon ~ x,y"
    TOL_DEG = 0.01  # ~1 km

    def fit(self, ref_df: pd.DataFrame):
        d = ref_df.dropna(subset=["latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"])
        X = d[["x_coordinate_state_plane", "y_coordinate_state_plane"]].to_numpy(float)
        self.m_lat = LinearRegression().fit(X, d["latitude"].to_numpy(float))
        self.m_lon = LinearRegression().fit(X, d["longitude"].to_numpy(float))
        self.lat_med, self.lon_med = d["latitude"].median(), d["longitude"].median()
        self.lat_mad = (d["latitude"] - self.lat_med).abs().median() * 1.4826
        self.lon_mad = (d["longitude"] - self.lon_med).abs().median() * 1.4826
        return self

    def _detect(self, batch):
        lat = pd.to_numeric(batch["latitude"], errors="coerce")
        lon = pd.to_numeric(batch["longitude"], errors="coerce")
        x = pd.to_numeric(batch["x_coordinate_state_plane"], errors="coerce")
        y = pd.to_numeric(batch["y_coordinate_state_plane"], errors="coerce")
        has_xy = x.notna() & y.notna() & lat.notna()
        resid = pd.Series(0.0, index=batch.index)
        if has_xy.any():
            X = np.c_[x[has_xy], y[has_xy]]
            resid[has_xy] = np.maximum(np.abs(lat[has_xy] - self.m_lat.predict(X)), np.abs(lon[has_xy] - self.m_lon.predict(X)))
        zfb = np.maximum(((lat - self.lat_med) / self.lat_mad).abs(), ((lon - self.lon_med) / self.lon_mad).abs())
        mask = (has_xy & (resid > self.TOL_DEG)) | (~has_xy & lat.notna() & (zfb > 8))
        alerts = [Alert(self.problem, "row", "lat/lon disagree with the row's own state-plane x/y "
                        f"(residual {resid[i]:.3f} deg)", row=int(i), column="latitude",
                        observed={"lat": lat[i], "lon": lon[i]}, expected="consistent with x/y", score=float(resid[i]))
                  for i in mask[mask].index[:_MAX_ALERTS]]
        return self.result(row_flags=mask, alerts=alerts)


class IsolationForestCoords(Detector):
    """Generic unsupervised outlier model on (lat, lon, x, y) — measured as an alternative, not the default."""
    name, family, problem, algorithm = "isolation_forest_coords", "ml", "lat_lon_swap", "IsolationForest(lat, lon, x, y)"

    def fit(self, ref_df: pd.DataFrame, seed: int = 0):
        from sklearn.ensemble import IsolationForest
        cols = ["latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"]
        d = ref_df.dropna(subset=cols)[cols].to_numpy(float)
        self.mu, self.sd = d.mean(0), d.std(0)
        self.model = IsolationForest(n_estimators=200, contamination=0.005, random_state=seed).fit((d - self.mu) / self.sd)
        self.cols = cols
        return self

    def _detect(self, batch):
        X = batch[self.cols].apply(pd.to_numeric, errors="coerce")
        ok = X.notna().all(axis=1)
        mask = pd.Series(False, index=batch.index)
        if ok.any():
            mask[ok] = self.model.predict((X[ok].to_numpy(float) - self.mu) / self.sd) == -1
        alerts = [Alert(self.problem, "row", "row is an outlier in (lat, lon, x, y) space", row=int(i), column="latitude",
                        observed={"lat": batch.at[i, "latitude"], "lon": batch.at[i, "longitude"]},
                        expected="inlier") for i in mask[mask].index[:_MAX_ALERTS]]
        return self.result(row_flags=mask, alerts=alerts)


def _house_number(addr: str) -> str:
    m = re.match(r"\s*(\d+[-\d]*)", addr or "")
    return m.group(1) if m else ""


class FuzzyNearDuplicate(Detector):
    name, family, problem, algorithm = "rapidfuzz_blocking", "ml", "near_duplicates", "blocking + rapidfuzz similarity"
    WINDOW_S, MIN_SIM = 3600, 88

    def _detect(self, batch):
        c = parse_dates(batch["created_date"])
        d = pd.DataFrame({"t": c, "ct": batch["complaint_type"].astype(str), "zip": batch["incident_zip"].astype(str),
                          "addr": batch["incident_address"].astype("string").fillna("").str.upper(),
                          "desc": batch["descriptor"].astype("string").fillna("").str.upper(),
                          "key": batch["unique_key"].astype(str)}).sort_values("t")
        flags = pd.Series(False, index=batch.index)
        scores: dict[int, float] = {}
        for _, g in d.groupby(["ct", "zip"]):
            if len(g) < 2:
                continue
            rows = list(g.itertuples())
            for i in range(1, len(rows)):
                cur = rows[i]
                if not cur.addr:
                    continue
                for j in range(i - 1, -1, -1):
                    prev = rows[j]
                    if (cur.t - prev.t).total_seconds() > self.WINDOW_S:
                        break
                    if prev.key == cur.key or not prev.addr:
                        continue
                    if _house_number(cur.addr) != _house_number(prev.addr):
                        continue  # different house numbers = different locations, however similar the street
                    s = min(fuzz.token_sort_ratio(cur.addr, prev.addr), fuzz.ratio(cur.desc, prev.desc))
                    if s >= self.MIN_SIM:
                        flags.at[cur.Index] = True
                        scores[cur.Index] = s
                        break
        alerts = [Alert(self.problem, "row", f"near-duplicate of an earlier request (similarity {scores[i]:.0f}, "
                        "same type/ZIP, within 60 min)", row=int(i), column="incident_address",
                        observed=batch.at[i, "incident_address"], expected="distinct incident", score=scores[i])
                  for i in list(scores)[:_MAX_ALERTS]]
        return self.result(row_flags=flags, alerts=alerts)


class ZipCentroidDistance(Detector):
    name, family, problem, algorithm = "zip_centroid_distance", "ml", "zip_borough_mismatch", "distance to ZIP centroid (spread units)"
    K = 6.0

    def fit(self, ref: Reference):
        self.cent = ref.zip_centroid
        self.zip_b = ref.zip_borough
        return self

    def _detect(self, batch):
        lat = pd.to_numeric(batch["latitude"], errors="coerce")
        lon = pd.to_numeric(batch["longitude"], errors="coerce")
        z = batch["incident_zip"].astype(str)
        dist = pd.Series(np.nan, index=batch.index)
        for i, (zz, a, o) in enumerate(zip(z, lat, lon)):
            c = self.cent.get(zz)
            if c is not None and a == a:
                dist.iloc[i] = max(abs(a - c[0]) / c[2], abs(o - c[1]) / c[3])
        unknown = batch["incident_zip"].notna() & z.map(lambda v: v not in self.cent)
        mask = (dist > self.K) | unknown
        alerts = [Alert(self.problem, "row", f"coordinates are {dist[i]:.1f} spreads from ZIP {z[i]}'s centroid"
                        if dist[i] == dist[i] else f"ZIP {z[i]} unknown in reference", row=int(i), column="incident_zip",
                        observed={"zip": z[i], "borough": batch.at[i, "borough"]},
                        expected=self.zip_b.get(z[i], "known ZIP"), score=float(dist[i]) if dist[i] == dist[i] else None)
                  for i in mask[mask].index[:_MAX_ALERTS]]
        return self.result(row_flags=mask.fillna(False), alerts=alerts)


class DescriptorClassifier(Detector):
    name, family, problem, algorithm = "tfidf_logreg_descriptor", "ml", "complaint_descriptor_mismatch", "TF-IDF char n-grams + LogisticRegression"
    P_MIN = 0.02

    def fit(self, ref_df: pd.DataFrame):
        pairs = ref_df.groupby(["descriptor", "complaint_type"]).size().reset_index(name="n")
        top = ref_df["complaint_type"].value_counts()
        self.classes_ok = set(top[top >= 20].index)
        pairs = pairs[pairs["complaint_type"].isin(self.classes_ok)]
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), lowercase=True, min_df=1)
        X = self.vec.fit_transform(pairs["descriptor"])
        self.clf = LogisticRegression(max_iter=2000, C=20.0)
        self.clf.fit(X, pairs["complaint_type"], sample_weight=np.sqrt(pairs["n"]))
        self.idx = {c: i for i, c in enumerate(self.clf.classes_)}
        return self

    def _detect(self, batch):
        desc = batch["descriptor"].fillna("")
        uniq = desc.unique()
        P = self.clf.predict_proba(self.vec.transform(uniq))
        lookup = {d: P[i] for i, d in enumerate(uniq)}
        probs = [lookup[d][self.idx[t]] if (t in self.idx and d) else np.nan for d, t in zip(desc, batch["complaint_type"])]
        p = pd.Series(probs, index=batch.index)
        mask = p < self.P_MIN
        alerts = []
        for i in mask[mask].index[:_MAX_ALERTS]:
            best = self.clf.classes_[int(np.argmax(lookup[desc[i]]))]
            alerts.append(Alert(self.problem, "row", f"descriptor looks like '{best}', not the stated type "
                                f"(P={p[i]:.3f})", row=int(i), column="descriptor",
                                observed={"type": batch.at[i, "complaint_type"], "descriptor": desc[i]},
                                expected=best, score=float(p[i])))
        return self.result(row_flags=mask.fillna(False), alerts=alerts)


class StatusTextClassifier(Detector):
    name, family, problem, algorithm = "tfidf_logreg_status", "ml", "resolution_status_contradiction", "TF-IDF word n-grams + LogisticRegression"
    P_MIN = 0.05

    def fit(self, ref_df: pd.DataFrame):
        d = ref_df.dropna(subset=["resolution_description"])
        d = d.assign(closed=(d["status"] == "Closed").astype(int))
        g = d.groupby(["resolution_description", "closed"]).size().reset_index(name="n")
        self.vec = TfidfVectorizer(ngram_range=(1, 3), min_df=1, sublinear_tf=True)
        X = self.vec.fit_transform(g["resolution_description"])
        self.clf = LogisticRegression(max_iter=2000, class_weight="balanced", C=5.0)
        self.clf.fit(X, g["closed"], sample_weight=np.log1p(g["n"]))
        return self

    def _detect(self, batch):
        text = batch["resolution_description"].fillna("")
        uniq = text.unique()
        p_closed = dict(zip(uniq, self.clf.predict_proba(self.vec.transform(uniq))[:, 1]))
        pc = text.map(p_closed)
        is_closed = batch["status"] == "Closed"
        p_stated = pc.where(is_closed, 1 - pc)
        mask = (p_stated < self.P_MIN) & (text != "")
        alerts = [Alert(self.problem, "row", f"resolution text implies {'closed' if pc[i] > .5 else 'open'} "
                        f"(P={pc[i]:.2f}) but status is {batch.at[i, 'status']}", row=int(i), column="status",
                        observed=batch.at[i, "status"], expected="Closed" if pc[i] > .5 else "not Closed",
                        score=float(p_stated[i])) for i in mask[mask].index[:_MAX_ALERTS]]
        return self.result(row_flags=mask, alerts=alerts)


class PIINovelty(Detector):
    name, family, problem, algorithm = "phonenumbers_oov_names", "statistical", "pii_in_text", "libphonenumber + OOV proper-noun bigrams"

    def fit(self, ref: Reference):
        self.vocab = set(ref.vocab)
        self.allow = set(ref.contact_allowlist)
        return self

    def _detect(self, batch):
        cache: dict[str, list] = {}
        hits = batch["resolution_description"].map(
            lambda t: cache.setdefault(t, phonenumbers_pii(t, self.vocab, self.allow)) if isinstance(t, str) else [])
        mask = hits.map(bool)
        alerts = [Alert(self.problem, "row", "free text contains " + ", ".join(sorted({k for k, _ in hits[i]})),
                        row=int(i), column="resolution_description", observed=[k for k, _ in hits[i]],
                        expected="template text without personal data") for i in mask[mask].index[:_MAX_ALERTS]]
        return self.result(row_flags=mask, alerts=alerts)


def build_family_b(ref: Reference, ref_df: pd.DataFrame) -> dict[str, Detector]:
    """One statistical-or-ML detector per problem, fitted on the reference period."""
    from project_b.statistical_detector import (ColumnProfileMatcher, DateShapeChangePoint, DuplicateRateTest,
                                                DurationTailTest, NullRateBinomial, TypeShareTest)
    dets = [ColumnProfileMatcher().fit(ref_df), TypeShareTest(), DateShapeChangePoint().fit(ref),
            CoordinateRegression().fit(ref_df), DuplicateRateTest().fit(ref), FuzzyNearDuplicate(),
            NullRateBinomial().fit(ref), DurationTailTest().fit(ref), ZipCentroidDistance().fit(ref),
            DescriptorClassifier().fit(ref_df), StatusTextClassifier().fit(ref_df), PIINovelty().fit(ref)]
    return {d.problem: d for d in dets}

