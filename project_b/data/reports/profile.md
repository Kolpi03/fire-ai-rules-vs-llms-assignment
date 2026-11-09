# NYC 311 (2023) raw-data profile

Sample: 328,529 rows from 37 of 37 planned sampled days (full year: 3,224,723 rows).

| Issue | Type | Definition | Rows (sample) | % of sample | Projected full year* |
|---|---|---|---:|---:|---:|
| due_date_mostly_null | completeness | due_date null (column effectively deprecated) | 327,210 | 99.60% | 3,211,776 |
| near_duplicate_requests | uniqueness | same complaint_type + incident_address within 60 minutes of an earlier request | 40,413 | 12.30% | 396,680 |
| mojibake_in_text | encoding | UTF-8 bytes decoded as Latin-1 in resolution text (e.g. 'Transportationâs') | 7,878 | 2.40% | 77,328 |
| resolution_updated_before_created | temporal | resolution_action_updated_date < created_date | 7,399 | 2.25% | 72,626 |
| closed_equals_created | temporal | closed_date == created_date to the second (auto-closed or placeholder) | 6,440 | 1.96% | 63,213 |
| closed_more_than_365_days_after_created | temporal | resolution took over a year | 5,439 | 1.66% | 53,387 |
| missing_coordinates | completeness | latitude or longitude null | 4,916 | 1.50% | 48,254 |
| missing_resolution_description | completeness | resolution_description null | 4,496 | 1.37% | 44,131 |
| missing_descriptor | completeness | descriptor null | 3,979 | 1.21% | 39,056 |
| missing_or_invalid_zip | validity | incident_zip null or not 5 digits | 3,775 | 1.15% | 37,054 |
| status_closed_without_closed_date | consistency | status == 'Closed' but closed_date is null | 2,152 | 0.66% | 21,123 |
| not_closed_but_has_closed_date | consistency | status != 'Closed' but closed_date is set | 601 | 0.18% | 5,899 |
| borough_unspecified | completeness | borough == 'Unspecified' or null | 328 | 0.10% | 3,220 |
| community_board_unspecified | completeness | community_board starts with '0 Unspecified' | 328 | 0.10% | 3,220 |
| closed_before_created | temporal | closed_date < created_date | 312 | 0.09% | 3,062 |
| created_at_exact_midnight | temporal | created_date exactly 00:00:00 (date-only timestamps) | 224 | 0.07% | 2,199 |
| possible_pii_in_resolution_text | privacy | non-agency phone/e-mail or out-of-vocabulary proper-noun pair in resolution text | 94 | 0.03% | 923 |
| city_case_or_spelling_variants | consistency | city values that differ only by case/whitespace from another value | 11 | 0.00% | 108 |
| zip_borough_mismatch | consistency | incident_zip's majority borough (>=95% of >=20 rows) differs from borough | 2 | 0.00% | 20 |
| coordinates_outside_nyc | validity | lat/lon outside the NYC bounding box | 0 | 0.00% | 0 |
| exact_duplicate_rows | uniqueness | row identical to another row in every column | 0 | 0.00% | 0 |
| duplicate_unique_key | uniqueness | unique_key appears more than once | 0 | 0.00% | 0 |
| latlon_inconsistent_with_xy | consistency | lat/lon more than 0.01 deg from the value implied by x/y state plane | 0 | 0.00% | 0 |

*Projection = sample share x full-year rows (estimate, not a count).

## Exact full-year counts (SoQL aggregates, where the API answered)

- total_rows: 3224723
- closed_before_created: 4313
- status_closed_without_closed_date: 20251
- missing_incident_zip: 38520
- missing_latitude: unavailable - request failed after 2 attempts: HTTPSConnectionPool(host='data.cityofnewyork.us
- borough_unspecified: 5994

## Examples (first 5 rows per issue; PII masked)

**due_date_mostly_null**
- `{"unique_key": "56419637", "agency": "NYPD", "complaint_type": "Noise - Residential"}`
- `{"unique_key": "56419644", "agency": "NYPD", "complaint_type": "Noise - Commercial"}`
- `{"unique_key": "56419659", "agency": "NYPD", "complaint_type": "Noise - Residential"}`

**near_duplicate_requests**
- `{"unique_key": "56419953", "created_date": "2023-01-03T01:18:23.000", "complaint_type": "Noise - Residential", "incident_address": "97-19 LEFFERTS BOULEVARD"}`
- `{"unique_key": "56419954", "created_date": "2023-01-03T01:51:37.000", "complaint_type": "Noise - Commercial", "incident_address": "44 EMPIRE BOULEVARD"}`
- `{"unique_key": "56420246", "created_date": "2023-01-03T00:54:11.000", "complaint_type": "Noise - Commercial", "incident_address": "37 VICTORY BOULEVARD"}`

**mojibake_in_text**
- `{"unique_key": "56425307", "resolution_description": "Service Request status for this request is available on the Department of Transportation\u00e2\u0080\u0099s website. Please click the \u00e2\u0080\u009cLearn More\u00e2\u0080\u009d link below."}`
- `{"unique_key": "56426211", "resolution_description": "Service Request status for this request is available on the Department of Transportation\u00e2\u0080\u0099s website. Please click the \u00e2\u0080\u009cLearn More\u00e2\u0080\u009d link below."}`
- `{"unique_key": "56426248", "resolution_description": "Service Request status for this request is available on the Department of Transportation\u00e2\u0080\u0099s website. Please click the \u00e2\u0080\u009cLearn More\u00e2\u0080\u009d link below."}`

**resolution_updated_before_created**
- `{"unique_key": "56426211", "agency": "DOT", "complaint_type": "Street Light Condition"}`
- `{"unique_key": "56426324", "agency": "DOT", "complaint_type": "Street Light Condition"}`
- `{"unique_key": "56426462", "agency": "DSNY", "complaint_type": "Derelict Vehicles"}`

**closed_equals_created**
- `{"unique_key": "56419835", "created_date": "2023-01-03T00:05:03.000", "closed_date": "2023-01-03T00:05:03.000", "agency": "DOHMH", "complaint_type": "Rodent"}`
- `{"unique_key": "56421944", "created_date": "2023-01-03T01:00:56.000", "closed_date": "2023-01-03T01:00:56.000", "agency": "DOHMH", "complaint_type": "Rodent"}`
- `{"unique_key": "56423635", "created_date": "2023-01-03T01:28:22.000", "closed_date": "2023-01-03T01:28:22.000", "agency": "DOHMH", "complaint_type": "Rodent"}`

**closed_more_than_365_days_after_created**
- `{"unique_key": "56420678", "agency": "DOHMH", "complaint_type": "Mobile Food Vendor"}`
- `{"unique_key": "56421059", "agency": "DOHMH", "complaint_type": "Mobile Food Vendor"}`
- `{"unique_key": "56422577", "agency": "DOHMH", "complaint_type": "Food Establishment"}`

**missing_coordinates**
- `{"unique_key": "56420853", "agency": "NYPD", "complaint_type": "Illegal Parking"}`
- `{"unique_key": "56422391", "agency": "DSNY", "complaint_type": "Derelict Vehicles"}`
- `{"unique_key": "56426091", "agency": "DSNY", "complaint_type": "Dirty Condition"}`

**missing_resolution_description**
- `{"unique_key": "56420575", "agency": "DOHMH", "complaint_type": "Rodent"}`
- `{"unique_key": "56423965", "agency": "DOHMH", "complaint_type": "Rodent"}`
- `{"unique_key": "56424591", "agency": "DSNY", "complaint_type": "Graffiti"}`

**missing_descriptor**
- `{"unique_key": "56419817", "agency": "DHS", "complaint_type": "Homeless Person Assistance"}`
- `{"unique_key": "56420336", "agency": "DHS", "complaint_type": "Homeless Person Assistance"}`
- `{"unique_key": "56420948", "agency": "DHS", "complaint_type": "Homeless Person Assistance"}`

**missing_or_invalid_zip**
- `{"unique_key": "56420948", "agency": "DHS", "complaint_type": "Homeless Person Assistance"}`
- `{"unique_key": "56422391", "agency": "DSNY", "complaint_type": "Derelict Vehicles"}`
- `{"unique_key": "56423901", "agency": "DHS", "complaint_type": "Homeless Person Assistance"}`

**status_closed_without_closed_date**
- `{"unique_key": "57041474", "status": "Closed", "closed_date": "<NA>"}`
- `{"unique_key": "57301902", "status": "Closed", "closed_date": "<NA>"}`
- `{"unique_key": "57306872", "status": "Closed", "closed_date": "<NA>"}`

**not_closed_but_has_closed_date**
- `{"unique_key": "56426495", "status": "Pending", "closed_date": "2022-12-30T09:34:00.000"}`
- `{"unique_key": "56426498", "status": "Pending", "closed_date": "2022-12-30T09:37:00.000"}`
- `{"unique_key": "56426894", "status": "Pending", "closed_date": "2022-12-30T09:42:00.000"}`

**borough_unspecified**
- `{"unique_key": "56426763", "agency": "DOT", "complaint_type": "Ferry Inquiry"}`
- `{"unique_key": "56428654", "agency": "DOT", "complaint_type": "Municipal Parking Facility"}`
- `{"unique_key": "56431165", "agency": "TLC", "complaint_type": "For Hire Vehicle Complaint"}`

**community_board_unspecified**
- `{"unique_key": "56426763", "agency": "DOT", "complaint_type": "Ferry Inquiry"}`
- `{"unique_key": "56428654", "agency": "DOT", "complaint_type": "Municipal Parking Facility"}`
- `{"unique_key": "56431165", "agency": "TLC", "complaint_type": "For Hire Vehicle Complaint"}`

**closed_before_created**
- `{"unique_key": "56426495", "created_date": "2023-01-03T09:34:00.000", "closed_date": "2022-12-30T09:34:00.000", "agency": "DOT"}`
- `{"unique_key": "56426498", "created_date": "2023-01-03T09:37:00.000", "closed_date": "2022-12-30T09:37:00.000", "agency": "DOT"}`
- `{"unique_key": "56426894", "created_date": "2023-01-03T09:42:00.000", "closed_date": "2022-12-30T09:42:00.000", "agency": "DOT"}`

**created_at_exact_midnight**
- `{"unique_key": "56422754", "agency": "DSNY", "complaint_type": "Derelict Vehicles"}`
- `{"unique_key": "56426360", "agency": "DOHMH", "complaint_type": "Food Poisoning"}`
- `{"unique_key": "56426375", "agency": "DOHMH", "complaint_type": "Food Poisoning"}`

**possible_pii_in_resolution_text**
- `{"unique_key": "56430477", "resolution_description": "The Department of Housing Preservation and Development was not able to gain access to inspect the conditions. If the conditions still exist and an inspection is"}`
- `{"unique_key": "56431000", "resolution_description": "The Department of Health and Mental Hygiene has investigated your complaint. You can get the status by calling <PHONE> on weekdays between 9 AM and 5 PM. This s"}`
- `{"unique_key": "56431717", "resolution_description": "The Department of Housing Preservation and Development was not able to gain access to inspect the conditions. If the conditions still exist and an inspection is"}`

**city_case_or_spelling_variants**
- `{"unique_key": "56786951", "city": "Brooklyn"}`
- `{"unique_key": "56873975", "city": "Brooklyn"}`
- `{"unique_key": "57214867", "city": "staten island"}`

**zip_borough_mismatch**
- `{"unique_key": "59686952", "incident_zip": "11416", "borough": "BROOKLYN"}`
- `{"unique_key": "59872206", "incident_zip": "11237", "borough": "QUEENS"}`
