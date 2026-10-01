# Geo route API: 30 test cases

These cases are executable in [`apps/geo/test_route_cases.py`](../apps/geo/test_route_cases.py). They use fixed coordinates, mocked station prices, and mocked routing data. They do not read the station CSV or call LocationIQ.

Run them with:

```sh
.venv/bin/python manage.py test apps.geo.test_route_cases
```

| # | Case | Expected result |
| --- | --- | --- |
| 01 | Missing `start` | Request rejected. |
| 02 | Missing `finish` | Request rejected. |
| 03 | Extra top-level field | Request rejected. |
| 04 | Start supplied as an address | Request rejected. |
| 05 | Finish supplied as an address | Request rejected. |
| 06 | Missing start `lat` | Request rejected. |
| 07 | Missing finish `lng` | Request rejected. |
| 08 | Extra coordinate field | Request rejected. |
| 09 | Boolean latitude | Request rejected. |
| 10 | String longitude | Request rejected. |
| 11 | Latitude above 90 | Request rejected. |
| 12 | Longitude below −180 | Request rejected. |
| 13 | Identical start and finish | Request rejected. |
| 14 | Malformed JSON body | Request rejected. |
| 15 | Point at the configured Kansas center | Inside the supported radius; no provider call. |
| 16 | Chicago coordinate | Inside the supported radius; no provider call. |
| 17 | Eastern Maine coordinate | Inside the supported radius; no provider call. |
| 18 | London coordinate | Outside the supported radius; no provider call. |
| 19 | Honolulu coordinate | Outside the current radius; no provider call. |
| 20 | Anchorage coordinate | Outside the current radius; no provider call. |
| 21 | 100-mile trip, no station | No fuel purchase. |
| 22 | 500-mile trip, no station | No fuel purchase. |
| 23 | 501-mile trip, no station | Unreachable with the full starting tank. |
| 24 | Stop at 400 miles, then 200 miles | Buy 10 gallons. |
| 25 | Stop at 500 miles, then 200 miles | Buy 20 gallons. |
| 26 | Cheaper second stop | Buy 10 gallons at the first, 20 at the second. |
| 27 | Cheaper first stop | Buy 30 gallons at the first, none at the second. |
| 28 | Any road leg over 500 miles | Plan rejected as unreachable. |
| 29 | Price of $3.335 for one gallon | Display $3.34 using half-up cent rounding. |
| 30 | Two reachable stations with different prices | Choose the cheaper fuel plan. |

The radius cases document the current approximation. They do not prove that a coordinate is within a US boundary. The route and fuel cases use fixed road miles so their expected values do not depend on live routing or station data.
