# Recovery around the confirmed working MediaMTX player

Branch: fix/raspberrypibase-native-player-recovery in TM_cell5.
This supersedes the direct-reader recovery branch, which did not connect on the
deployed Pi. It restores the same embedded MediaMTX player that worked after the
user's rollback. It does not use the replacement reader.

## Apply to the existing raspberrypibase installation

Run as aegixcore, without sudo on Git:

```bash
cd /home/aegixcore/raspberrypibase &&
git fetch origin fix/raspberrypibase-native-player-recovery &&
TASK_UPGRADE=$(mktemp /tmp/tm-native-preview.XXXXXX.py) &&
git show FETCH_HEAD:scripts/upgrade-native-preview.py > "$TASK_UPGRADE" &&
python3 "$TASK_UPGRADE"
```

The updater verifies the installed service directory before modifying source.
It recognises only the exact app.js/index.html rollback to the confirmed baseline
16e680d. Those local edits are saved in Git stash before switching branches. Other
tracked edits cause it to stop without discarding them. A clean checkout is also
accepted. The application service and its archive configuration are retained.

The installer backs up its files under /var/backups/tm-camera, copies a small local
observer extension and launcher wrapper under /usr/local/lib/tm-camera, and adds
96-native-player.conf to the existing kiosk service. The installed session-aware
launcher is imported unchanged; the wrapper adds only Chromium extension and
console logging flags. The separate kiosk account and graphical session discovery
remain in use. Only the kiosk browser restarts. Neither the recording server nor
MediaMTX is restarted, reconfigured or replaced.

The installer waits up to 40 seconds for BOTH observer heartbeat and decoded video
progress in the new kiosk service invocation's journal. It does not report success
merely because a service is active. If validation fails, its previous kiosk files
are restored and the browser restarts with the prior configuration. The webpage
still uses the original MediaMTX player. Send the reported error if it fails.
There is no need to wait on site for a several-hour outage to install this, but
check the picture after installation.

## Recovery behavior

The iframe continues to load http://127.0.0.1:8889/pramacam/ (or localhost). The only
URL addition is a generation token in the query string. The MediaMTX page's HTML,
reader, WebRTC negotiation and peer connection remain its own unmodified code.

Chromium's isolated content script reads the existing video element's decoded
frame callbacks, or decoded frame counters on older browsers. It posts progress
once per second to the local dashboard. It never changes srcObject, starts/stops
tracks or creates a peer connection. An empty motionless cell still produces new
decoded frames and does not trigger a reload.

After the observer connects, 20 seconds without initial frames, or 10 seconds
without further frames, unloads and reloads ONLY the live iframe. Delays between
failed retries back off from 2 seconds to at most 30 seconds. Healthy playback is
not periodically reloaded. Leaving Live or hiding the dashboard closes the iframe;
returning starts the native player again. Recording playback is independent.

The dashboard verifies message origin, source window, generation token and numeric
frame counters. It ignores old messages after reload. No remote debugging port,
disabled browser security, additional web service or new WebRTC client is used.

The observer is scoped to loopback HTTP URLs and activates only for the pramacam
iframe on port 8889 with the generation token. It has no background worker,
storage, camera, microphone or filesystem permissions. Loading an unpacked local
extension is supported by Chromium; official Chrome-branded builds differ. The
existing Pi launcher uses Chromium. See the primary references below.

If an extension is unavailable or blocked, the working native player is left
alone and the console explicitly reports that automatic monitoring is disabled.
The installer rejects an unverified observer instead of claiming recovery works.
JavaScript cannot recover a completely hung browser process, power failure, or
permanently unreachable camera. Long-duration operation on the actual Pi has not
been verified by local automated tests.

## Validation and diagnosis

```bash
python3 -m unittest discover -s tests -v
node tests/test_native_player.cjs
sudo journalctl -u tm-camera-kiosk.service -n 80 --no-pager
```

Node is needed only for developer tests. Tests cover 72 simulated healthy hours
without a reload, frozen frames, startup failures, 100 failed retries, origin and
generation filtering, missing-observer behavior, frame counter fallback and
cleanup. Python tests cover rollback preservation, verification failure rollback,
browser-only restarts and the existing recording/storage/HTTP/deployment suite.

Expected journal markers are TM_NATIVE_OBSERVER_READY and TM_NATIVE_VIDEO_PROGRESS.
These confirm observation and real decoded progress on that particular browser;
they are not a guarantee that no future hardware or browser failure can occur.

To disable this observer and retain the working player, run:

```bash
sudo mv /etc/systemd/system/tm-camera-kiosk.service.d/96-native-player.conf "/var/tmp/tm-native-player-$(date +%Y%m%d-%H%M%S).conf"
sudo systemctl daemon-reload
sudo systemctl restart tm-camera-kiosk.service
```

The player will still work, but automatic observation is disabled. To reinstall,
run sudo python3 scripts/apply-native-preview.py from this checkout.

Primary references:
- https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts
- https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY/m/S0ET5wPjCAAJ
