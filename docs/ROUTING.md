# Routing and Geocoding

JobRadar separates two different tasks:

1. **Geocoding** — converting an address such as `Regensburg, Germany` into latitude/longitude coordinates.
2. **Routing** — calculating driving distance and travel time between those coordinates.

Both are configured under **Settings → Routing/Geocoding**.

---

## Recommended configuration

> ✅ **Recommended for most users**
>
> ```text
> Geocoding provider: ors
> Routing provider:   ors
> ```

openrouteservice (ORS) provides hosted geocoding and routing behind one API key and is operated by HeiGIT. Create your account and API key through the HeiGIT Account portal: account.heigit.org. JobRadar uses the ORS API hosted at api.heigit.org.

Official links:

- ORS login/account: https://account.heigit.org/login
- ORS public API overview: https://api.heigit.org

> ✅ The ORS services is **free** to use but has some limitations (see [Standard plan](https://account.heigit.org/info/plans)), which are usually not exceeded.

## ORS setup

1. Create/log in to your heigit (maintains openrouteservice) account:
   https://account.heigit.org/login
2. Create an API key and copy the value.
3. Store the secret value in an environment variable. JobRadar defaults to:
   ```text
   ORS_API_KEY
   ```
4. Restart terminals/IDEs that were already open.
5. Open **JobRadar → Settings → Routing/Geocoding**.
6. Set:
   ```text
   Geocoding provider: ors
   Routing provider:   ors
   ORS API env var:    ORS_API_KEY
   ```
7. Save settings (via **Save and Close** button).
8. Calculate a route for one known job and verify that the result is plausible.

---

## Privacy: choose your home/base location deliberately

> ⚠️ Routing/geocoding requests send location information to an external service.

You usually do not need to enter your exact apartment/house location.

Consider using:

- your street without the house number,
- a nearby intersection,
- a nearby side street,
- another point close enough for realistic commuting estimates.

This slightly reduces precision but avoids unnecessarily sending your exact home address with every uncached request.

JobRadar caches geocoding and route results, so identical locations normally do not need to be queried repeatedly.

---

# Available geocoding providers

## `ors` — recommended

Uses the openrouteservice geocoding API.

**Advantages**

- one account/API key can cover both geocoding and routing,
- suitable for repeated JobRadar use,
- avoids relying on the public Nominatim instance for bulk-ish workflows.

**Requirements**

- ORS account,
- API key,
- API quotas/limits apply.

---

## `nominatim`

Uses the public OpenStreetMap Foundation Nominatim server:

```text
https://nominatim.openstreetmap.org/search
```

Nominatim converts address strings into coordinates. It does **not** calculate JobRadar's route distance by itself.

The public service has an explicit usage policy:

https://operations.osmfoundation.org/policies/nominatim/

Important limitations include:

- maximum 1 request per second,
- no heavy use,
- repeated/bulk geocoding is discouraged,
- applications should cache results,
- service access may be withdrawn or blocked if usage violates the policy.

JobRadar does cache addresses and rate-limits requests, but Company Watch or a large imported result set can still create many unique geocoding requests.

💡 **Recommendation:** Nominatim is fine for light/personal testing, but ORS is the safer default for regular JobRadar use.

---

# Available routing providers

## `ors` — recommended

Uses openrouteservice driving directions.

This gives an actual road-route distance and duration rather than a geometric estimate.

**Advantages**

- proper road routing,
- authenticated API,
- one provider can also be used for geocoding,
- generally the simplest reliable configuration.

**Limitations**

- requires an API key,
- ORS quotas/rate limits apply,
- external service availability is outside JobRadar's control.

---

## `osrm_demo`

Uses the public OSRM demo endpoint:

```text
https://router.project-osrm.org/
```

It calculates a real road route from the geocoded coordinates.

**Advantages**

- no account/API key required,
- useful for testing.

**Limitations**

- it is a public/demo service,
- it can throttle, block or become unavailable under heavier use,
- it should not be treated as guaranteed infrastructure for large batches.

OSRM's public community servers also publish conservative usage rules such as approximately one request per second and no heavy usage.

💡 **Recommendation:** Use `osrm_demo` for occasional testing, not as the preferred backend for processing many jobs.

---

## `direct`

`direct` does **not** query a road-routing service.

It:

1. geocodes the start and destination,
2. calculates straight-line (great-circle/Haversine) distance,
3. multiplies that distance by a fixed road factor,
4. estimates travel time from a fixed average speed.

The current implementation uses approximately:

```text
road factor:        1.25
average speed:      75 km/h
```

Therefore the result is only an **estimate**.

**Advantages**

- no routing API request,
- fast after geocoding,
- useful fallback when exact routing is unnecessary.

**Limitations**

- rivers, mountains, road layout and border crossings are not represented,
- travel time is only a rough estimate,
- a 40 km straight-line destination may have a much longer real road route.

💡 **Recommendation:** Useful for rough pre-filtering, but ORS is preferable when commute distance matters.

---

# Combining geocoding and routing providers

The settings are independent.

Examples:

```text
Nominatim geocoding + OSRM Demo routing
ORS geocoding       + ORS routing
ORS geocoding       + Direct estimate
```

For simplicity, the recommended normal configuration is:

```text
ORS + ORS
```

---

# Multiple job locations

Some job advertisements contain several office locations.

JobRadar can keep these locations separately and automatically chooses the closest one (using the direct method). The main-table location context menu can be used to select a different location manually from the available options. Location entries in the main table, which have multiple location options, are prepended with a `*` symbol.

When changing a location, JobRadar should invalidate the old route display so it can be recalculated for the new destination.

---

# Cache behavior

JobRadar caches:

- geocoded coordinates,
- route calculations.

Caching is important because it:

- speeds up repeated work,
- avoids unnecessary API requests,
- reduces the chance of hitting public-service rate limits.

If an address was interpreted incorrectly, use the route/location context-menu action to delete the relevant cache entry and calculate it again.

> ⚠️ If you change your home address this usually means that **ALL cached routes become invalid** and must be re-calculated! So choose your home address wisely during the initial setup and never touch it again. 

---

# Troubleshooting

## Route looks obviously wrong

Possible causes:

- malformed location supplied by the job portal,
- geocoder selected the wrong place,
- stale cached coordinates,
- multi-location job selected the wrong office.

Try:

1. inspect/set the job location,
2. delete the route/geocoding cache entry,
3. recalculate.

## OSRM Demo hangs or fails

This can happen with public/demo infrastructure.

Switch to ORS for regular use.

## Nominatim starts rejecting requests

Stop bulk requests and check the current Nominatim usage policy:

https://operations.osmfoundation.org/policies/nominatim/

For regular JobRadar use, switch geocoding to ORS.

## ORS says the API key is missing

JobRadar reads the environment variable whose **name** is configured in:

```text
Settings → Routing/Geocoding → ORS API env var
```

Default:

```text
ORS_API_KEY
```

Make sure the environment variable is visible to the process that starts JobRadar. If you created it while PyCharm/a terminal was already running, restart that application.
