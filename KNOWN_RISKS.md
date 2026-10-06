# Known security and operational-safety risks

- Audit date: 2026-10-06
- Reviewed commit: `e93907381b9bf0f76697dbf9096aaad66d8a44fc`
- Integration version: `2.0.0-beta.1`
- Assumed installation: two parallel 8 kW Sunsynk inverters in a master/slave configuration.

This register records audit findings and remediation progress. The behaviour descriptions below describe the original audited commit; progress notes describe the current implementation. It does not implement fixes or establish that a particular inverter, firmware, battery, or BMS accepts every command described. Source line references refer to the reviewed commit and may move as fixes are made.

**Assessment:** unattended write control should remain blocked until the High findings are resolved and the master/slave behaviour is validated. No Critical vulnerability, deliberate credential exfiltration, or reachable backend arbitrary-code-execution path was established in this audit. That is not a general assurance that the integration is safe.

## Evidence and limitations

- The audit covered credential handling, outbound communication, HTTP POST operations, write callers, routing, bounds, concurrency, recovery, filesystem access, bundled JavaScript, and repository supply-chain configuration.
- Sixteen offline probes exercised actual source definitions with mocked Home Assistant and cloud interfaces. They confirmed the code behaviours identified below; they did not validate physical inverter behaviour.
- No Sunsynk credentials were used and no inverter was contacted or changed.
- The full pytest suite was not run because pytest and Home Assistant were absent from the audit environment.
- The deployed Home Assistant version, dependency inventory, firmware, battery configuration, BMS limits, and cloud propagation behaviour were not available.
- Published dependency advisories were checked during the audit. Reachability and installed-version qualifications are preserved below.
- The temporary probe script was not added to this repository. Its results are summarized in this document; the proposed verification cases below should become durable regression coverage when each issue is addressed.

## Remediation progress

- [x] Default new and existing entries to read-only, with a shared write guard at coordinator and API boundaries. Explicit read/write mode and installation profiles are required for inverter control.
- [x] R02: require declared group membership, a verified master, matching plant/roles, known capabilities, and fresh topology before writes. Missing or failed topology blocks writes.
- [x] R03: send minimal battery/system payloads, read timer companions fresh, validate every transmitted field, reject detected concurrent changes, and verify affected sibling settings.
- [x] R04: enforce explicit current/power/export/reserve limits, hardware power caps, SOC relationships and timer ordering; remove automatic 30 kW and SOC fallback values.
- [x] Add regression coverage and document configuration in README and CHANGELOG. The full mocked Home Assistant suite passes: 574 tests, 100% statement coverage; production Ruff checks pass.
- [ ] Validate R02–R04 with a real/test inverter and the actual installation limits, firmware and cloud payload semantics before closing these findings.
- [ ] R01: enforce custom-service caller authorization. Read-only mode reduces exposure while enabled but does not supply caller permission checks in read/write mode.
- [ ] R05: persist original settings and uncertain operation/recovery state across restart. Runtime ownership is now recorded before writes and retained after failed restoration, but durable recovery remains open.
- [ ] R06–R17: complete the remaining findings and their verification cases below. R08 now stops later groups after failure and reports possible partial application; compensating recovery remains open.

The implementation commit is discoverable with `git log -- KNOWN_RISKS.md`. No inverter was contacted during implementation. Real Home Assistant/inverter validation and CI hassfest/HACS validation remain pending.

## Risk register

Status distinguishes implemented safeguards from physical validation. **Implemented; hardware validation pending** does not mean the finding is closed. Use the stable IDs below when recording fixes and regression coverage.

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| R01 | High | Custom services omit caller permission checks | Open |
| R02 | High | Master/slave routing fails open with missing topology | Implemented; hardware validation pending |
| R03 | High | Group writes resend stale and unvalidated sibling settings | Implemented; hardware validation pending |
| R04 | High | Bounds do not enforce installation-specific safety limits | Implemented; hardware validation pending |
| R05 | High | Ambiguous writes and restoration failures lose recovery state | Open |
| R06 | High | Virtual slots can start without a complete restoration baseline | Open |
| R07 | High | Future weekday schedules lose calendar semantics | Open |
| R08 | High | Multi-step transitions can leave partially applied settings | Open |
| R09 | High | Tariff decisions can use stale SOC after polling failure | Open |
| R10 | Medium | Coalescing and cancellation can misrepresent write outcomes | Open |
| R11 | Medium | Current changes and controller handoffs retain stale values | Open |
| R12 | Medium | Early cloud read-back does not verify durable parallel convergence | Open |
| R13 | Medium | Bundled Lodash version has published vulnerabilities | Open |
| R14 | Medium | Authentication and write requests follow unrestricted redirects | Open |
| R15 | Medium | Non-finite prices pass quality checks | Open |
| R16 | Low | Logs retain unsanitized upstream errors and device identifiers | Open |
| R17 | Low | Optional dashboard management can overwrite local configuration | Open |

### R01 — High: custom services omit caller permission checks

**Behaviour and impact:** the `force_charge`, `force_discharge`, `set_work_mode`, and virtual-slot service handlers look up a configured serial and act without checking `call.context.user_id`, administrator status, or permission to control the resolved inverter. They are registered as ordinary services. An authenticated HA user with restricted entity access can potentially bypass those restrictions by calling the custom services with a known configured serial. This was established by source review and HA's permission model, not an end-to-end restricted-user test.

**Evidence:** [__init__.py:291](custom_components/sunsynk/__init__.py#L291), [__init__.py:319](custom_components/sunsynk/__init__.py#L319), [__init__.py:350](custom_components/sunsynk/__init__.py#L350). [HA service permission documentation](https://developers.home-assistant.io/docs/auth_permissions/#securing-a-service-action-handler) explains the checks required for custom handlers.

**Remediation:** check caller authorization against the resolved physical target before changing settings or schedules. Decide which sensitive actions require administrator access.

**Verification before closure:** a user without inverter control permission cannot invoke any of these services; authorized users and intended system automations still work; requesting the slave does not bypass authorization for the master.

### R02 — High: master/slave routing fails open with missing topology

**Behaviour and impact:** missing `parallel` metadata makes an inverter directly writable. A known slave also resolves to itself if no master is found. Partial endpoint failures replace failed inverter metadata with `{}`, so a normal cloud outage can enable direct slave writes. The resolver chooses the first configured parallel master without checking plant or parallel-group identity, creating a wrong-target risk when additional groups are configured.

**Evidence:** [api/client.py:287](custom_components/sunsynk/api/client.py#L287), [coordinator.py:102](custom_components/sunsynk/coordinator.py#L102), [coordinator.py:245](custom_components/sunsynk/coordinator.py#L245).

**Offline result:** losing either unit's relevant topology metadata allowed direct slave routing.

**Remediation:** require verified group membership and a known master. Block writes when topology is missing, stale, or ambiguous; do not infer independence from missing data.

**Verification before closure:** simulate loss of each topology endpoint, slave-only configuration, topology changes, and multiple parallel groups. No write may fall back to an unverified target or be duplicated on one master.

### R03 — High: group writes resend stale and unvalidated sibling settings

**Behaviour and impact:** changing one setting resends cached sibling settings from its API group. A fresh read occurs only when the cache is empty. Newer changes made through the inverter or cloud app can therefore be overwritten. The battery group includes voltages, equalization settings, `bmsErrStop`, `lithiumMode`, and `safetyType`, but validation only covers the narrower `WRITABLE_SETTING_KEYS` set. Other transmitted fields pass through unchecked. Verification checks requested changes, not all resent siblings.

**Evidence:** [coordinator.py:398](custom_components/sunsynk/coordinator.py#L398), [coordinator.py:420](custom_components/sunsynk/coordinator.py#L420), [coordinator.py:431](custom_components/sunsynk/coordinator.py#L431), [coordinator.py:453](custom_components/sunsynk/coordinator.py#L453), [const.py:865](custom_components/sunsynk/const.py#L865).

**Offline result:** a current write resent a stale sibling current and an unvalidated `absorptionVolt=999`. This confirms payload generation, not firmware acceptance.

**Remediation:** minimize write payloads. If the cloud requires whole groups, read fresh, validate every transmitted field, and detect concurrent changes rather than overwriting them silently.

**Verification before closure:** an external change between polling and writing is preserved or explicitly rejected as a conflict; invalid voltage/protection fields cannot reach a POST as cached siblings.

### R04 — High: bounds do not enforce installation-specific safety limits

**Behaviour and impact:** generic validation accepts up to 300 A and 30,000 W without checking inverter capabilities, battery/BMS limits, configured maximum currents, or site export limits. SOC thresholds are independently accepted within 0–100%, without relationship checks. Timer strings are checked for syntax without validating complete six-slot ordering. The scheduler defaults to 30 kW when rated power is unknown, exceeding both one 8 kW unit and this installation's combined 16 kW rating. Firmware may clamp or reject commands, but the integration does not establish that protection. AC inverter power alone is insufficient to determine a safe DC battery-current limit.

**Evidence:** [write_validation.py:9](custom_components/sunsynk/write_validation.py#L9), [write_validation.py:116](custom_components/sunsynk/write_validation.py#L116), [virtual_slots.py:594](custom_components/sunsynk/virtual_slots.py#L594).

**Offline result:** generic validation accepted 300 A, 30 kW, and contradictory shutdown/restart SOC values. Missing rated power selected the 30 kW fallback.

**Remediation:** require validated installation limits and cross-field constraints. Block automatic power writes when required capabilities are unknown. Establish whether each setting applies per inverter or across the parallel group.

**Verification before closure:** every write path, including cached siblings and restoration, obeys the configured installation envelope; unknown capabilities and contradictory thresholds fail before any POST.

### R05 — High: ambiguous writes and restoration failures lose recovery state

**Behaviour and impact:** tariff ownership is recorded only after a write and verification succeed. If the inverter accepts a POST but verification fails, the possibly applied override is not tracked. Restoration catches failures and then clears ownership sets. Unload ignores returned failure status and removes the managers. Scheduler snapshots are in memory, while persisted data contains only virtual schedules. Both managers initialize disabled. A crash or restart without successful restoration can leave device settings active while HA has lost the state needed to recover them. A disabled switch does not prove restoration succeeded.

**Evidence:** [tariff.py:102](custom_components/sunsynk/tariff.py#L102), [tariff.py:192](custom_components/sunsynk/tariff.py#L192), [tariff.py:448](custom_components/sunsynk/tariff.py#L448), [virtual_slots.py:276](custom_components/sunsynk/virtual_slots.py#L276), [virtual_slots.py:327](custom_components/sunsynk/virtual_slots.py#L327), [__init__.py:592](custom_components/sunsynk/__init__.py#L592).

**Offline result:** failed tariff restoration cleared ownership. A simulated accepted-but-unverified tariff write was not tracked and received no restoration attempt during shutdown.

**Remediation:** durably record original settings and operation intent before writes. Treat uncertain outcomes as pending reconciliation. Retain failed restoration state, retry recovery, and reconcile on startup before accepting new automatic control.

**Verification before closure:** inject timeouts after POST acceptance, failed restoration, process termination at each transition, reload, and restart. Recovery state survives and HA accurately reports unresolved device state.

### R06 — High: virtual slots can start without a complete restoration baseline

**Behaviour and impact:** missing snapshot fields cause a warning but do not stop the scheduler from disabling slots 2–5 and rewriting managed slots. Original charge/discharge currents are not captured. If normal currents are unconfigured, ending a virtual window or disabling the scheduler does not restore those registers.

**Evidence:** [virtual_slots.py:391](custom_components/sunsynk/virtual_slots.py#L391), [virtual_slots.py:455](custom_components/sunsynk/virtual_slots.py#L455), [virtual_slots.py:812](custom_components/sunsynk/virtual_slots.py#L812).

**Offline result:** bootstrap wrote settings with an empty restoration snapshot. With default `None` normal-current values, a virtual-slot current received no restoration write.

**Remediation:** require a complete, fresh, validated restoration baseline before taking ownership, with an explicit policy for both current registers and coordination with tariff ownership.

**Verification before closure:** incomplete snapshots prevent all bootstrap writes; ending or disabling a standalone schedule restores both currents according to the declared policy.

### R07 — High: future weekday schedules lose calendar semantics

**Behaviour and impact:** the resolver calculates a future calendar boundary, but `_plan()` reduces it to `HH:MM` and enables the corresponding physical slot immediately. It does not program matching weekday restrictions. A future-day window can therefore run on an earlier day when that day is enabled in the physical timer.

**Evidence:** [virtual_slots.py:548](custom_components/sunsynk/virtual_slots.py#L548), [virtual_slots.py:659](custom_components/sunsynk/virtual_slots.py#L659), [virtual_slots.py:758](custom_components/sunsynk/virtual_slots.py#L758).

**Offline result:** Tuesday at noon with a Monday-only 10:00–14:00 virtual schedule generated an enabled physical slot starting at `10:00`, while the intended next boundary was the following Monday. Firmware execution was not tested.

**Remediation:** preserve calendar semantics during translation and do not pre-enable future-day windows as recurring daily timers. Define what the physical schedule must do if HA stops updating it.

**Verification before closure:** test weekday gaps, next-day windows, midnight transitions, and HA downtime; the physical program cannot activate before the intended date or persist beyond its intended validity unnoticed.

### R08 — High: multi-step transitions can leave partially applied settings

**Behaviour and impact:** grouped settings are sent as separate POSTs. A later failure leaves earlier writes applied, without compensating recovery. Scheduler transitions update slot 1, slot 6, the slot-2 boundary, and finally current limits in separate operations. A new window can be enabled while old current limits or boundaries remain in effect.

**Evidence:** [coordinator.py:419](custom_components/sunsynk/coordinator.py#L419), [coordinator.py:446](custom_components/sunsynk/coordinator.py#L446), [virtual_slots.py:701](custom_components/sunsynk/virtual_slots.py#L701).

**Offline result:** a multi-group batch reported failure after its current change had already succeeded, with no rollback.

**Remediation:** implement a recoverable transition protocol that establishes safe limits before activation, records partial completion, and reconciles failures. Do not describe a batch failure as implying no settings changed.

**Verification before closure:** fail every POST/read-back position in a transition and confirm that partial state is tracked and recovered without briefly activating an unsafe combination.

### R09 — High: tariff decisions can use stale SOC after polling failure

**Behaviour and impact:** complete polling failure raises `UpdateFailed` instead of publishing the locally cleared battery data. Tariff evaluation uses cached SOC without checking coordinator success or measurement age. Stale but numerically valid SOC can initiate an override or sustain a discharge decision beyond its intended reserve.

**Evidence:** [coordinator.py:165](custom_components/sunsynk/coordinator.py#L165), [coordinator.py:208](custom_components/sunsynk/coordinator.py#L208), [tariff.py:369](custom_components/sunsynk/tariff.py#L369).

**Offline result:** an evaluation with `last_update_success=False` and cached SOC still issued a charging-current write.

**Remediation:** require successful, sufficiently fresh safety telemetry for automatic decisions. Define and track the recovery action when telemetry becomes stale or unavailable.

**Verification before closure:** complete/partial polling failures, authentication failures, and unchanged old measurements cannot initiate or silently sustain an override based on stale SOC.

### R10 — Medium: coalescing and cancellation can misrepresent write outcomes

**Behaviour and impact:** same-turn writes merge with `pending.update(settings)`, while waiters retain keys rather than requested values. Conflicting requests can both report success although only the final value was transmitted. Cancelling a caller does not remove its queued write. Locks belong to one coordinator instance, so separate entries controlling the same physical inverter do not share serialization.

**Evidence:** [coordinator.py:68](custom_components/sunsynk/coordinator.py#L68), [coordinator.py:318](custom_components/sunsynk/coordinator.py#L318), [coordinator.py:354](custom_components/sunsynk/coordinator.py#L354).

**Offline result:** two conflicting current requests both succeeded with one final value written; a cancelled caller's queued write still executed.

**Remediation:** distinguish applied, superseded, cancelled, and uncertain results. Detect conflicting values, define cancellation semantics, and coordinate writes by physical target across entries.

**Verification before closure:** exercise same-key conflicts, cancellation before dispatch and during POST, duplicate configuration entries, and simultaneous service/entity/scheduler writes.

### R11 — Medium: current changes and controller handoffs retain stale values

**Behaviour and impact:** changing a tariff current triggers evaluation, but the write condition excludes already-active inverters. Lowering an active current setting therefore does not apply it. Separately, virtual-slot current deduplication is not invalidated while tariff overrides own the register. Returning to the same virtual slot can skip reapplying its current after tariff restoration changed the device value.

**Evidence:** [tariff.py:436](custom_components/sunsynk/tariff.py#L436), [tariff.py:542](custom_components/sunsynk/tariff.py#L542), [virtual_slots.py:805](custom_components/sunsynk/virtual_slots.py#L805), [virtual_slots.py:834](custom_components/sunsynk/virtual_slots.py#L834).

**Offline result:** both an active tariff-current reduction and re-entry into the same virtual slot skipped the required write.

**Remediation:** reconcile desired values against verified device state, not only active flags or the last request issued by one controller. Explicitly transfer ownership and invalidate deduplication on handoff.

**Verification before closure:** active limit reductions reach the device; tariff-to-virtual and virtual-to-tariff transitions apply the intended current exactly once and preserve safe ordering.

### R12 — Medium: early cloud read-back does not verify durable parallel convergence

**Behaviour and impact:** verification exits on the first matching cloud response, with retry delays of 0.25, 0.5, and 1.25 seconds. It checks requested fields on the resolved target, without confirming slave convergence or later persistence. The repository itself describes observed reversions after 10–15 seconds. Those observations were not independently reproduced on hardware during this audit.

**Evidence:** [coordinator.py:44](custom_components/sunsynk/coordinator.py#L44), [coordinator.py:235](custom_components/sunsynk/coordinator.py#L235), [coordinator.py:498](custom_components/sunsynk/coordinator.py#L498).

**Remediation:** distinguish command acknowledgment from verified application. Establish a firmware-appropriate observation period and confirm required fields converge across the parallel group without issuing duplicate writes.

**Verification before closure:** delayed application, early cloud echo, later reversion, and divergent slave state are not reported as durable success.

### R13 — Medium: bundled Lodash version has published vulnerabilities

**Behaviour and impact:** the bundled dashboard card identifies Lodash `4.17.23` and includes its `template`, `unset`, and `omit` implementations. Published advisories cover template-import code injection and prototype-property deletion, with fixes starting at `4.18.0`. The card executes in the HA frontend context when loaded.

**Reachability qualification:** the vulnerable component is present, but this audit did not establish attacker-controlled input reaching the affected functions through the card. This is not a demonstrated browser or HA takeover. The constant `Function("return this")` global-object fallback in bundled library code is not itself evidence of an attacker-controlled execution path.

**Evidence:** [sunsynk-power-flow-card.js:63](custom_components/sunsynk/www/sunsynk-power-flow-card.js#L63), [frontend registration at __init__.py:549](custom_components/sunsynk/__init__.py#L549). Primary advisories: [CVE-2026-4800](https://github.com/lodash/lodash/security/advisories/GHSA-r5fr-rjxr-66jc) and [CVE-2026-2950](https://github.com/lodash/lodash/security/advisories/GHSA-f23m-r3pf-42rh).

**Supply-chain limitation:** the repository contains the minified card but not its source/build manifest and lockfile, limiting reproducibility and dependency-review coverage.

**Remediation and verification:** rebuild from reviewed source with patched dependencies, remove unused vulnerable functionality where possible, record source/version provenance, and verify the contents of the resulting bundle rather than relying only on a version string.

### R14 — Medium: authentication and write requests follow unrestricted redirects

**Behaviour and impact:** requests leave aiohttp's automatic redirects enabled. A server-controlled 307/308 can forward the POST body to another origin, including login username/encrypted-password material or inverter settings. Destination and scheme are not constrained by the integration on redirect.

**Qualification:** this requires an upstream redirect; none was observed against Sunsynk during this audit because no live cloud tests were performed. Cross-origin bearer-header stripping is separate from POST-body forwarding. The finding does not establish plaintext password exposure.

**Evidence:** [api/auth.py:82](custom_components/sunsynk/api/auth.py#L82), [api/client.py:111](custom_components/sunsynk/api/client.py#L111). See [aiohttp request defaults](https://docs.aiohttp.org/en/stable/client_reference.html) and [redirect implementation](https://github.com/aio-libs/aiohttp/blob/master/aiohttp/client.py).

**Remediation and verification:** reject redirects for authentication and write operations, or explicitly validate every destination and scheme. Mock cross-origin and HTTPS-to-HTTP redirects and confirm no sensitive body is forwarded.

### R15 — Medium: non-finite prices pass quality checks

**Behaviour and impact:** price quality checks accept anything `float()` parses, including infinity and NaN. Negative infinity satisfies cheap-rate comparisons and positive infinity can satisfy expensive-rate comparisons.

**Evidence:** [tariff.py:246](custom_components/sunsynk/tariff.py#L246), [tariff.py:260](custom_components/sunsynk/tariff.py#L260).

**Offline result:** `-inf` was classified as valid and caused a charging-current write.

**Remediation and verification:** require finite prices and test NaN, both infinities, unavailable states, and stale values. Rejecting non-finite values must not inadvertently reject legitimate finite negative prices.

### R16 — Low: logs retain unsanitized upstream errors and device identifiers

**Behaviour and impact:** authentication failures log the cloud's `msg` verbatim. API error messages flow into logged exceptions, and serials are routinely logged. If the upstream echoes sensitive material, it can enter local logs and support attachments without redaction.

**Qualification:** no direct logging of configured passwords or bearer tokens was found. The reflected-secret case is conditional, not an observed credential leak.

**Evidence:** [api/auth.py:101](custom_components/sunsynk/api/auth.py#L101), [api/client.py:51](custom_components/sunsynk/api/client.py#L51), [api/client.py:289](custom_components/sunsynk/api/client.py#L289).

**Remediation and verification:** sanitize upstream error text and sensitive identifiers. Test synthetic errors containing credential-like values without using real secrets.

### R17 — Low: optional dashboard management can overwrite local configuration

**Behaviour and impact:** when dashboard creation is enabled, setup saves generated content over the existing integration dashboard. The fallback directly loads, modifies, and saves shared `lovelace_dashboards` storage, creating a possible lost-update conflict with other writers. This is a configuration-integrity risk, not an arbitrary filesystem traversal primitive.

**Evidence:** [__init__.py:680](custom_components/sunsynk/__init__.py#L680), [__init__.py:731](custom_components/sunsynk/__init__.py#L731).

**Remediation and verification:** avoid replacing user-edited content without a deliberate regeneration action and use supported registry/storage coordination. Test preservation of edited dashboards and concurrent changes to unrelated dashboard registrations.

## HTTP POST and write inventory

These are all outbound POST endpoint constructions found in the reviewed integration.

| Endpoint | Data and triggers | Source |
| --- | --- | --- |
| `/oauth/token/new` | Username, RSA-encrypted password, and protocol fields during credential validation and token renewal | [auth.py:70](custom_components/sunsynk/api/auth.py#L70) |
| `/api/v1/common/setting/{serial}/set` | Inverter settings from custom services, number/switch/text entities, tariff decisions, virtual slots, and restoration | [client.py:211](custom_components/sunsynk/api/client.py#L211) |
| `/api/v1/plant/{plant_id}/income` | Manual plant energy-price edits, resending currency, investment, and charge metadata | [client.py:227](custom_components/sunsynk/api/client.py#L227) |

The plant-price path permits edits only to an existing single constant-price entry and checks plant metadata. It requests a refresh after writing but does not explicitly compare the resulting price with the requested price. Its lock is keyed by resolved inverter rather than plant, which does not serialize separate independent inverter targets sharing one plant. These limitations should be covered when improving write coordination. See [coordinator.py:597](custom_components/sunsynk/coordinator.py#L597), [coordinator.py:654](custom_components/sunsynk/coordinator.py#L654), and [coordinator.py:697](custom_components/sunsynk/coordinator.py#L697).

Manual `force_charge` and `force_discharge` actions change persistent current registers; they do not supply a duration or automatic expiry. Disabling the Tariff Manager or Virtual Slot Scheduler does not disable manual entity writes or the custom services. The audited commit had no integration-wide read-only gate. The current implementation defaults to read-only and guards both inverter-setting and plant-price writes; authentication and monitoring remain available. See [__init__.py:291](custom_components/sunsynk/__init__.py#L291) and [switch.py:236](custom_components/sunsynk/switch.py#L236).

## Informational observations and remaining checks

### I01 — Credential storage and diagnostics

The original password is stored in config-entry data and retained in memory. RSA encryption applies to the authentication request payload, not to storage of the configured password. HA storage and backups therefore contain sensitive configuration. Diagnostics redact explicitly named credential fields and known identifiers, but this is not an exhaustive guarantee against arbitrary future API fields containing sensitive information.

Evidence: [config_flow.py:121](custom_components/sunsynk/config_flow.py#L121), [auth.py:26](custom_components/sunsynk/api/auth.py#L26), [auth.py:131](custom_components/sunsynk/api/auth.py#L131), [diagnostics.py:21](custom_components/sunsynk/diagnostics.py#L21).

### I02 — Network destinations and TLS

The configuration offers `api.sunsynk.net` and `pv.inteless.com`. Forecasting additionally sends latitude and longitude to `https://api.open-meteo.com/v1/forecast`; the forecast request does not attach Sunsynk credentials. No additional hard-coded telemetry or credential-exfiltration destination was identified in the reviewed runtime code. TLS verification is not explicitly disabled. Redirect handling remains R14.

Evidence: [const.py:32](custom_components/sunsynk/const.py#L32), [config_flow.py:55](custom_components/sunsynk/config_flow.py#L55), [coordinator.py:713](custom_components/sunsynk/coordinator.py#L713), [coordinator.py:753](custom_components/sunsynk/coordinator.py#L753).

### I03 — Backend execution and filesystem access

No backend `eval`, shell/subprocess execution, dynamic code download, pickle loading, or cloud-controlled filesystem path was identified. The manifest read uses a fixed local path. Scheduler storage keys hash the serial instead of placing it directly into a filesystem path. Calibration storage uses the HA config-entry identifier. Optional dashboard storage mutation remains R17, and bundled browser dependency risk remains R13.

Evidence: [__init__.py:134](custom_components/sunsynk/__init__.py#L134), [virtual_slots.py:268](custom_components/sunsynk/virtual_slots.py#L268), [calibration.py:34](custom_components/sunsynk/calibration.py#L34).

### I04 — Supply chain and deployed dependencies

Workflow actions are pinned to commit hashes. The release workflow itself does not depend on completed tests and has no artifact-attestation step. Test requirements include packages with open lower bounds and no hashes. Runtime Python libraries such as aiohttp and cryptography are supplied by the HA environment, with no separate runtime requirements declared in this integration's manifest. These observations do not establish an exploitable Python dependency vulnerability in the user's deployment; that requires its actual inventory. The bundled Lodash finding is recorded separately as R13.

Evidence: [dependency-review.yml:13](.github/workflows/dependency-review.yml#L13), [release.yaml:11](.github/workflows/release.yaml#L11), [requirements_test.txt:1](requirements_test.txt#L1), [manifest.json:1](custom_components/sunsynk/manifest.json#L1), [auth.py:10](custom_components/sunsynk/api/auth.py#L10).

### I05 — Existing controls worth retaining

The integration has HTTPS requests with timeouts, explicit write-value validation, per-target write locks within a coordinator, strict matching of known API success messages, read-back checks, and diagnostic redaction. Tariff automation and the virtual scheduler initialize disabled. These controls reduce some risks but do not resolve the findings above.

Evidence: [api/client.py:16](custom_components/sunsynk/api/client.py#L16), [api/client.py:103](custom_components/sunsynk/api/client.py#L103), [write_validation.py:85](custom_components/sunsynk/write_validation.py#L85), [coordinator.py:275](custom_components/sunsynk/coordinator.py#L275), [tariff.py:102](custom_components/sunsynk/tariff.py#L102), [virtual_slots.py:276](custom_components/sunsynk/virtual_slots.py#L276).

## Suggested remediation order

1. Add an enforceable read-only/write-enable gate, fix service authorization, and require verified master/group routing.
2. Establish installation-specific safety limits and remove stale or unvalidated sibling writes.
3. Persist operation/recovery state and implement safe handling of partial and ambiguous outcomes.
4. Correct virtual schedule calendar handling, telemetry freshness checks, and controller handoffs.
5. Fix conflict/cancellation semantics and validate delayed parallel convergence.
6. Rebuild the vulnerable frontend bundle, constrain redirects, and address logging/dashboard integrity issues.

When closing a finding, record the fixing commit, regression evidence, and any remaining hardware/cloud validation requirement. Do not mark a physical-safety finding resolved solely because mocked tests pass. Follow [SECURITY.md](SECURITY.md) for external vulnerability reporting; this register does not authorize publication or live inverter testing.
