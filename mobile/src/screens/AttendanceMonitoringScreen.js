/**
 * Attendance Monitoring Screen.
 * Shows the live session and lets a student verify their face for it.
 *
 * Two steps, in this order, and the server refuses them out of order:
 *
 * 1. **Check in with the classroom code.** The instructor's attendance page
 *    shows a six-digit code that changes every 6 seconds. Typing it proves
 *    the phone is in the room; it gives a pass for a
 *    few minutes (services/classroom_code.py). Without it the app could be
 *    used from home.
 *
 * 2. **Verify the face - a stream of frames, not one photo.** The server runs
 *    each frame through the classroom camera's own rules (services/
 *    phone_verification.py): a window of matches against the student's
 *    captured face images, a frontal face, a two-step head-turn challenge and
 *    a re-check. Each answer carries the instruction, plus `stage`, `step`
 *    and `progress`, which this screen draws as an arrow and a bar, until the
 *    server says `done`, refuses, or VERIFY_TIMEOUT_MS runs out.
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import { View, StyleSheet, Text, ScrollView, RefreshControl, Alert } from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { useFocusEffect } from '@react-navigation/native';
import { Ionicons } from '@expo/vector-icons';
import client from '../api/client';
import Button from '../components/Button';
import Header from '../components/Header';
import Card from '../components/Card';
import Input from '../components/Input';
import {
  captureFrame,
  frameFormData,
  FRAME_ASPECT,
  MAX_FRAMES_PER_REQUEST,
  smallestUsablePictureSize,
} from '../utils/frame';
import { colors, spacing, typography, borderRadius } from '../theme/colors';

// Long enough for the window, two head turns (8 s each at most) and the
// re-check on a slow phone; short enough that a student in the dark is told
// why rather than left waiting. Shorter than the check-in pass (180 s).
const VERIFY_TIMEOUT_MS = 90000;

// Consecutive frames that failed to reach the server before giving up.
const MAX_CONSECUTIVE_FAILURES = 5;

// Waiting while MAX_PHONES other students verify (the server's 429).
const BUSY_RETRY_MS = 2000;

const LIGHTING_TIPS = [
  'Face a light or a window - not with a window behind you.',
  'Hold the phone at eye level and fit your face inside the square.',
  'Follow the arrow: look straight, then turn your head when asked.',
];

// The head turn the server is asking for (vision/liveness.py), as the
// student sees it. The front preview is a mirror, so "your left" is the
// screen's left: the arrow points the way the face on screen should move.
//
// "and hold": a phone sends a picture every half second or so, and a quick
// turn and back can fall between two of them and never be seen - measured
// 2026-10-24, the turns took 13 s. Holding until the arrow changes is what
// makes the step register on the first try.
const STEP_PROMPTS = {
  LEFT: { icon: 'arrow-back', text: 'Turn your head to your left and hold' },
  RIGHT: { icon: 'arrow-forward', text: 'Turn your head to your right and hold' },
  CENTER: { icon: 'scan-outline', text: 'Look straight at the camera and hold' },
};

const STAGE_LABELS = {
  finding: 'Looking for your face',
  matching: 'Checking it is you',
  liveness: 'Liveness check',
  recheck: 'Face the camera - almost done',
  done: 'Done',
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export default function AttendanceMonitoringScreen() {
  const [liveData, setLiveData] = useState(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  // 'idle' (code entry or button), 'face' (front camera)
  const [mode, setMode] = useState('idle');
  const [isVerifying, setIsVerifying] = useState(false);
  const [instruction, setInstruction] = useState('');
  const [problem, setProblem] = useState('');
  const [progress, setProgress] = useState({ stage: 'finding', step: null, value: 0 });
  const [passUntil, setPassUntil] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [typedCode, setTypedCode] = useState('');
  const [isCheckingIn, setIsCheckingIn] = useState(false);
  const [permission, requestPermission] = useCameraPermissions();
  const cameraRef = useRef(null);
  const [pictureSize, setPictureSize] = useState(undefined);

  // takePictureAsync() before the camera is ready throws; the loop waits.
  const cameraReadyRef = useRef(false);

  // The loop reads this rather than `isVerifying`: state is a snapshot from
  // the render that started the loop and would never see the stop.
  const verifyingRef = useRef(false);

  const hasPass = passUntil > now;

  const handleCameraReady = async () => {
    cameraReadyRef.current = true;

    // Small pictures, not the sensor's full 12+ MP - see
    // smallestUsablePictureSize(). Asked once per opening of the camera.
    if (mode === 'face' && pictureSize === undefined && cameraRef.current) {
      try {
        const sizes = await cameraRef.current.getAvailablePictureSizesAsync();
        const chosen = smallestUsablePictureSize(sizes);
        if (chosen) setPictureSize(chosen);
      } catch {
        // Keep the default size: slower, but it works.
      }
    }
  };

  const fetchLiveData = async () => {
    try {
      const response = await client.get('/api/attendance/live');
      if (response.data.success) {
        setLiveData(response.data);
        // The server is the authority on the pass; this keeps a reopened
        // screen from asking for the code again while it is still valid.
        const seconds = response.data.pass_seconds || 0;
        setPassUntil(seconds > 0 ? Date.now() + seconds * 1000 : 0);
      }
    } catch (err) {
      console.log('Error fetching live data:', err);
    }
  };

  useEffect(() => {
    fetchLiveData();
    const interval = setInterval(fetchLiveData, 5000); // poll every 5s
    return () => clearInterval(interval);
  }, []);

  // The pass countdown.
  useEffect(() => {
    if (!passUntil) return undefined;
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [passUntil]);

  const stopVerifying = useCallback((message = '') => {
    verifyingRef.current = false;
    setIsVerifying(false);
    setProblem(message);
  }, []);

  // Leaving the tab or the screen stops the stream; the bottom tabs keep this
  // screen mounted, so unmounting alone would not.
  useFocusEffect(
    useCallback(() => () => {
      stopVerifying();
      setMode('idle');
    }, [stopVerifying])
  );

  // The session ended under us - the camera view is about to go away.
  useEffect(() => {
    if (!liveData?.running && verifyingRef.current) {
      stopVerifying('The attendance session has ended.');
    }
    if (!liveData?.running) setMode('idle');
  }, [liveData, stopVerifying]);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    await fetchLiveData();
    setIsRefreshing(false);
  };

  const ensureCameraPermission = async () => {
    if (permission?.granted) return true;
    const result = await requestPermission();
    return result.granted;
  };

  // -- step 1: the classroom code -------------------------------------------

  const checkIn = async (code) => {
    setIsCheckingIn(true);
    setProblem('');

    try {
      const response = await client.post('/api/attendance/checkin', { code });
      setPassUntil(Date.now() + response.data.pass_seconds * 1000);
      setNow(Date.now());
      setTypedCode('');
      // Straight on to the face: the pass is running.
      if (!(await ensureCameraPermission())) return;
      cameraReadyRef.current = false;
      setInstruction('');
      setProgress({ stage: 'finding', step: null, value: 0 });
      setMode('face');
    } catch (err) {
      const data = err.response?.data || {};
      let message = data.message || 'Could not check in: ' + err.message;
      if (data.tries_left !== undefined) {
        message += ` (${data.tries_left} tries left)`;
      }
      setProblem(message);
    } finally {
      setIsCheckingIn(false);
    }
  };

  // -- step 2: the face ---------------------------------------------------------

  const verify = async () => {
    verifyingRef.current = true;
    setIsVerifying(true);
    setProblem('');
    setInstruction('Starting...');
    setProgress({ stage: 'finding', step: null, value: 0 });

    const deadline = Date.now() + VERIFY_TIMEOUT_MS;
    let failures = 0;

    // Pictures waiting for the next request, and how long each took - see
    // the loop below.
    const queue = [];
    const captureTimes = [];

    // The last thing the server asked for ("liveness LEFT", "recheck", ...),
    // and when its answer arrived: pictures started before then are dropped.
    let lastAsk = '';
    let askChangedAt = 0;

    // One frame's round trip. Resolves 'next' to keep going, or 'stop' once
    // verification is over (done, refused, or given up) - never rejects.
    const send = async (photos, captureMs) => {
      try {
        const response = await client.post('/api/attendance/frame', frameFormData(photos), {
          headers: {
            'Content-Type': 'multipart/form-data',
            // How long this phone takes per picture - logged by the server
            // beside its own timings, to tell a slow camera from a slow
            // network (services/phone_verification.py _log_timing).
            'X-Capture-Ms': String(captureMs),
          },
        });

        if (!verifyingRef.current) return 'stop';

        const data = response.data;
        failures = 0;

        // ⚠️ **A new instruction makes the waiting pictures useless.** They
        // were taken while this request was out - before the student could
        // see "Turn LEFT" - so the next request would carry no turn, and the
        // turn would reach the server a whole request later: ~10 s on the
        // user's 4G, past the liveness step's 8 s, and the challenge was
        // redrawn four times in one attempt (2026-10-25). Dropping them sends
        // only pictures taken after the arrow appeared. Nothing about what
        // the server accepts changes.
        const ask = `${data.stage || ''} ${data.step || ''}`;
        if (ask !== lastAsk) {
          lastAsk = ask;
          askChangedAt = Date.now();
          queue.length = 0;
          captureTimes.length = 0;
        }

        setInstruction(data.message);
        setProgress((previous) => ({
          stage: data.stage || previous.stage,
          step: data.step || null,
          value: typeof data.progress === 'number' ? data.progress : previous.value,
        }));

        if (data.done) {
          stopVerifying();
          setMode('idle');
          Alert.alert(data.already ? 'Already recorded' : 'Verified!', data.message);
          fetchLiveData();
          return 'stop';
        }

        return 'next';
      } catch (err) {
        if (!verifyingRef.current) return 'stop';

        const status = err.response?.status;
        const data = err.response?.data || {};
        const message = data.message;

        // The check-in pass ran out (or the server restarted): back to the
        // code, which is quick, rather than a dead end.
        if (data.need_code) {
          stopVerifying('Your check-in expired. Enter the classroom code again.');
          setPassUntil(0);
          setMode('idle');
          return 'stop';
        }

        if (status === 429) {
          setInstruction(message);
          await sleep(BUSY_RETRY_MS);
          return 'next';
        }

        // Answers no further frame can change: no session, not in this
        // class, signed out.
        if (status === 401 || status === 403 || status === 409) {
          stopVerifying(message || 'Verification is not available right now.');
          return 'stop';
        }

        failures += 1;

        if (failures >= MAX_CONSECUTIVE_FAILURES) {
          stopVerifying(
            'Verification stopped: ' + (message || err.message || 'the server could not be reached.')
          );
          return 'stop';
        }

        await sleep(500);
        return 'next';
      }
    };

    // ⚠️ **The camera never waits for the network.** One round trip through
    // the tunnel costs 0.7-1.3 s whatever the picture's size (measured
    // 2026-10-24), so sending one picture per trip held the phone to about
    // one frame a second. Pictures are taken back to back into `queue`; while
    // a request is out, they wait, and the next request carries all of them
    // (up to MAX_FRAMES_PER_REQUEST, the newest). Still one request at a time
    // and oldest first, so the server sees the frames in order and the
    // instruction on screen is never out of date.
    let inFlight = false;
    let finished = false;

    const sendQueued = () => {
      if (inFlight || finished || queue.length === 0) return;

      inFlight = true;
      const batch = queue.splice(0, queue.length);
      const times = captureTimes.splice(0, captureTimes.length).sort((a, b) => a - b);
      const captureMs = times.length ? times[Math.floor(times.length / 2)] : 0;

      send(batch, captureMs).then((outcome) => {
        inFlight = false;
        if (outcome === 'stop') finished = true;
      });
    };

    while (verifyingRef.current && !finished) {
      if (Date.now() > deadline) {
        stopVerifying(
          'Could not verify you in time. Check the tips below and try again.'
        );
        return;
      }

      if (!cameraRef.current || !cameraReadyRef.current) {
        await sleep(300);
        continue;
      }

      try {
        // Cropped to the square the preview shows - see ../utils/frame.js.
        const started = Date.now();
        const photo = await captureFrame(cameraRef.current);

        // Begun before the latest instruction arrived: the same stale
        // picture as the ones dropped in send().
        if (started >= askChangedAt) {
          queue.push(photo);
          captureTimes.push(Date.now() - started);
        }
      } catch (err) {
        failures += 1;

        if (failures >= MAX_CONSECUTIVE_FAILURES) {
          stopVerifying('Verification stopped: the camera could not take a picture.');
          return;
        }

        await sleep(300);
        continue;
      }

      // A slow network: keep the newest pictures, which are what the student
      // is doing now.
      if (queue.length > MAX_FRAMES_PER_REQUEST) {
        queue.splice(0, queue.length - MAX_FRAMES_PER_REQUEST);
      }

      sendQueued();
    }
  };

  const openFaceCamera = async () => {
    if (!(await ensureCameraPermission())) return;
    setProblem('');
    setInstruction('');
    cameraReadyRef.current = false;
    setMode('face');
  };

  if (!liveData?.running) {
    return (
      <View style={styles.container}>
        <Header title="Attendance Monitoring" />
        <ScrollView
          contentContainerStyle={styles.center}
          refreshControl={<RefreshControl refreshing={isRefreshing} onRefresh={handleRefresh} />}
        >
          <Ionicons name="videocam-off-outline" size={64} color={colors.textSecondary} />
          <Text style={styles.noSessionTitle}>No Live Session</Text>
          <Text style={styles.noSessionSubtitle}>
            There is no active attendance session running right now.
          </Text>
        </ScrollView>
      </View>
    );
  }

  const stepPrompt = progress.stage === 'liveness' ? STEP_PROMPTS[progress.step] : null;
  const passSecondsLeft = Math.max(Math.ceil((passUntil - now) / 1000), 0);

  const renderCameraArea = () => {
    if (mode === 'face') {
      return (
        <View style={styles.avatarContainer}>
          <View style={styles.avatarBox}>
            <CameraView
              style={StyleSheet.absoluteFill}
              facing="front"
              ref={cameraRef}
              onCameraReady={handleCameraReady}
              pictureSize={pictureSize}
              // ⚠️ The saved picture is not mirrored, like the classroom
              // camera's, so "turn LEFT" is the student's own left on both
              // (vision/liveness.py). Set explicitly so a default cannot
              // swap the two directions on some phones.
              mirror={false}
              // No shutter flash on every frame: it is a stream of
              // pictures, and the animation is time each one waits.
              animateShutter={false}
            />
            {/* Where the face goes. Drawn over the preview only - the
                whole square around it is still what is sent. */}
            <View pointerEvents="none" style={styles.faceGuide} />
            {isVerifying && stepPrompt ? (
              <View pointerEvents="none" style={styles.stepOverlay}>
                <Ionicons name={stepPrompt.icon} size={72} color="#FFFFFF" />
              </View>
            ) : null}
          </View>
          <Text style={styles.framingHint}>
            Fit your face inside the square - only what you can see here is sent.
          </Text>
        </View>
      );
    }

    return (
      <View style={styles.avatarContainer}>
        <View style={styles.avatarBox}>
          <Ionicons
            name={hasPass ? 'person-outline' : 'keypad-outline'}
            size={96}
            color={colors.secondary}
          />
        </View>
      </View>
    );
  };

  const renderActions = () => {
    // Once a day per class: a student the register already holds is told so
    // instead of being offered another attempt. The server refuses a second
    // attempt anyway (/api/attendance/frame); this is the screen agreeing.
    if (liveData.your_status) {
      return (
        <Card style={styles.recordedCard}>
          <Ionicons name="checkmark-circle" size={28} color={colors.statusPresent} />
          <Text style={styles.recordedText}>
            You are already recorded as {liveData.your_status} for this class today.
            Attendance can only be taken once.
          </Text>
        </Card>
      );
    }

    if (mode === 'face') {
      return (
        <>
          {isVerifying ? (
            <View style={styles.progressBlock}>
              <Text style={styles.stageLabel}>
                {STAGE_LABELS[progress.stage] || STAGE_LABELS.finding}
              </Text>
              <View style={styles.progressTrack}>
                <View
                  style={[styles.progressFill, { width: `${Math.round(progress.value * 100)}%` }]}
                />
              </View>
              {stepPrompt ? (
                <Text style={styles.instruction}>{stepPrompt.text}</Text>
              ) : instruction ? (
                <Text style={styles.instruction}>{instruction}</Text>
              ) : null}
            </View>
          ) : null}

          {hasPass ? (
            <Text style={styles.passText}>
              Checked in - {passSecondsLeft} s left to verify
            </Text>
          ) : null}

          {isVerifying ? (
            <Button
              title="Stop"
              variant="outline"
              onPress={() => stopVerifying()}
              style={styles.joinButton}
            />
          ) : (
            <Button
              title="Verify My Face"
              onPress={verify}
              disabled={!hasPass}
              style={styles.joinButton}
            />
          )}
        </>
      );
    }

    return hasPass ? (
      <>
        <Text style={styles.passText}>Checked in - {passSecondsLeft} s left to verify</Text>
        <Button title="Verify My Face" onPress={openFaceCamera} style={styles.joinButton} />
      </>
    ) : (
      <>
        <Text style={styles.stepHint}>
          Step 1 of 2: type the 6-digit classroom code shown on your instructor's screen.
        </Text>
        <Input
          placeholder="Classroom code"
          value={typedCode}
          onChangeText={(text) => setTypedCode(text.replace(/\D/g, '').slice(0, 6))}
          keyboardType="number-pad"
          icon="keypad-outline"
        />
        <Button
          title="Check In"
          onPress={() => checkIn(typedCode)}
          loading={isCheckingIn}
          disabled={typedCode.length !== 6}
          style={styles.joinButton}
        />
      </>
    );
  };

  return (
    <View style={styles.container}>
      <Header title="Attendance Monitoring" />
      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl refreshing={isRefreshing} onRefresh={handleRefresh} />}
        keyboardShouldPersistTaps="handled"
      >
        <Text style={styles.sectionTitle}>Live Attendance Session</Text>
        <Text style={styles.subjectCode}>{liveData.subject_code}</Text>

        {liveData.your_status ? null : renderCameraArea()}

        {problem ? <Text style={styles.problem}>{problem}</Text> : null}

        {renderActions()}

        {mode === 'face' && (
          <Card style={styles.tipsCard}>
            <Text style={styles.tipsTitle}>For a quick match</Text>
            {LIGHTING_TIPS.map((tip) => (
              <Text key={tip} style={styles.tipText}>{'•'} {tip}</Text>
            ))}
          </Card>
        )}

        <View style={styles.statsRow}>
          <Text style={styles.statsText}>
            Recognized so far: <Text style={styles.statsHighlight}>{liveData.recognised}</Text>
          </Text>
        </View>

        <Text style={styles.listTitle}>Recently Recognized</Text>
        {liveData.students.length === 0 ? (
          <Text style={styles.emptyText}>No students recognized yet.</Text>
        ) : (
          liveData.students.map((student) => (
            <Card key={student.student_id} style={styles.studentCard}>
              <View style={styles.studentInfo}>
                <Text style={styles.studentName}>{student.name}</Text>
                <Text style={styles.studentId}>{student.student_id}</Text>
              </View>
              <View style={styles.studentStatus}>
                <Text style={styles.timeText}>{student.time_in}</Text>
                <Text
                  style={[
                    styles.statusText,
                    { color: student.status === 'Late' ? colors.statusLate : colors.statusPresent },
                  ]}
                >
                  {student.status}
                </Text>
              </View>
            </Card>
          ))
        )}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  center: {
    flexGrow: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: spacing.xl,
  },
  noSessionTitle: {
    ...typography.h2,
    marginTop: spacing.lg,
    marginBottom: spacing.xs,
  },
  noSessionSubtitle: {
    ...typography.body,
    color: colors.textSecondary,
    textAlign: 'center',
  },
  content: {
    flexGrow: 1,
    padding: spacing.lg,
  },
  sectionTitle: {
    ...typography.h3,
    color: colors.secondary,
    marginBottom: spacing.sm,
  },
  subjectCode: {
    ...typography.h2,
    color: colors.secondary,
    marginBottom: spacing.lg,
  },
  avatarContainer: {
    alignItems: 'center',
    marginBottom: spacing.xl,
  },
  // ⚠️ FRAME_ASPECT (square), matching the crop in ../utils/frame.js. A
  // CameraView covers its box, so this shape *is* the window that gets
  // uploaded - and the server letterboxes it rather than cropping it. Change
  // the shape here without changing both and a framed face is cut away
  // server-side again (lessons.md L33).
  avatarBox: {
    width: '85%',
    aspectRatio: FRAME_ASPECT,
    backgroundColor: colors.primary,
    justifyContent: 'center',
    alignItems: 'center',
    overflow: 'hidden',
  },
  // The head-turn arrow, over the preview where the student is looking.
  stepOverlay: {
    position: 'absolute',
    bottom: spacing.md,
    backgroundColor: 'rgba(0, 0, 0, 0.45)',
    borderRadius: borderRadius.md,
    padding: spacing.sm,
  },
  progressBlock: {
    marginBottom: spacing.md,
  },
  stageLabel: {
    ...typography.bodySmall,
    fontWeight: '700',
    color: colors.secondary,
    textAlign: 'center',
    marginBottom: spacing.xs,
  },
  progressTrack: {
    height: 10,
    borderRadius: 5,
    backgroundColor: colors.border,
    overflow: 'hidden',
    marginBottom: spacing.sm,
  },
  progressFill: {
    height: '100%',
    backgroundColor: colors.success,
  },
  instruction: {
    ...typography.h3,
    color: colors.secondary,
    textAlign: 'center',
    marginBottom: spacing.md,
  },
  problem: {
    ...typography.bodySmall,
    color: colors.error,
    textAlign: 'center',
    marginBottom: spacing.md,
  },
  passText: {
    ...typography.caption,
    color: colors.success,
    textAlign: 'center',
    marginBottom: spacing.sm,
  },
  stepHint: {
    ...typography.bodySmall,
    color: colors.textSecondary,
    textAlign: 'center',
    marginBottom: spacing.md,
  },
  tipsCard: {
    padding: spacing.md,
    marginBottom: spacing.xl,
  },
  tipsTitle: {
    ...typography.bodySmall,
    fontWeight: '700',
    marginBottom: spacing.xs,
  },
  tipText: {
    ...typography.caption,
    marginBottom: 2,
  },
  // The face guide: a square inset from the preview's edges, so a face inside
  // it is whole, clear of the edge of what is sent, and not so close that it
  // covers more of the frame than the geometry gate allows.
  faceGuide: {
    position: 'absolute',
    width: '72%',
    aspectRatio: 1,
    borderWidth: 3,
    borderColor: colors.primary,
    borderRadius: borderRadius.md,
  },
  framingHint: {
    ...typography.caption,
    color: colors.textSecondary,
    textAlign: 'center',
    marginTop: spacing.sm,
  },
  statsRow: {
    alignItems: 'center',
    marginBottom: spacing.xl,
  },
  statsText: {
    ...typography.body,
    color: colors.textSecondary,
  },
  statsHighlight: {
    ...typography.h3,
    color: colors.secondary,
  },
  recordedCard: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: spacing.md,
    marginBottom: spacing.xl,
  },
  recordedText: {
    ...typography.body,
    flex: 1,
    marginLeft: spacing.sm,
  },
  joinButton: {
    marginBottom: spacing.md,
  },
  listTitle: {
    ...typography.h3,
    marginBottom: spacing.md,
  },
  emptyText: {
    ...typography.body,
    color: colors.textSecondary,
    fontStyle: 'italic',
  },
  studentCard: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: spacing.sm,
    padding: spacing.md,
  },
  studentInfo: {
    flex: 1,
  },
  studentName: {
    ...typography.body,
    fontWeight: '600',
  },
  studentId: {
    ...typography.caption,
  },
  studentStatus: {
    alignItems: 'flex-end',
  },
  timeText: {
    ...typography.caption,
    marginBottom: 4,
  },
  statusText: {
    ...typography.bodySmall,
    fontWeight: '700',
  },
});
