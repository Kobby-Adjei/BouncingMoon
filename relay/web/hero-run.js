// Generated from hero-run.json (a real recorded run). Do not edit by hand.
(function () {
"use strict";
window.RELAY_HERO_RUN = Object.freeze({
 "about": "A real Relay run, recorded on a local hub: a person, two agents and a worker node (@studio, running relay/cli.py node --cap digest --cap clip). Chat lines from you, claude and muse were posted by the recording script; claude's numbers come from the job results. Lines from relay are the hub's own. Times are seconds from the first message.",
 "recorded": "2026-11-19T00:05:38Z",
 "source": {
  "name": "big-buck-bunny.mp4",
  "bytes": 33082947,
  "duration": 90.066,
  "width": 1920,
  "height": 1080,
  "trim": "first 90 s of bbb_sunflower_1080p_30fps_normal.mp4 (download.blender.org), stream copy",
  "credit": "Big Buck Bunny © Blender Foundation, CC BY 3.0",
  "credit_url": "https://peach.blender.org/"
 },
 "messages": [
  {
   "id": 1,
   "from": "you",
   "t": 0.0,
   "text": "@claude find the best moments in this video",
   "job": null,
   "files": [],
   "by": "script"
  },
  {
   "id": 2,
   "from": "claude",
   "t": 0.01,
   "text": "job j_5a05b0c4b914 [digest] artifact=aa951457fe26a981e747213e5d581a84b8018cccdd27b148a2299bb941867680",
   "job": "j_5a05b0c4b914",
   "files": [],
   "by": "script"
  },
  {
   "id": 3,
   "from": "relay",
   "t": 0.19,
   "text": "@studio picked up job j_5a05b0c4b914 [digest]",
   "job": "j_5a05b0c4b914",
   "files": [],
   "by": "hub"
  },
  {
   "id": 4,
   "from": "relay",
   "t": 8.65,
   "text": "job j_5a05b0c4b914 [digest] done by @studio: 90.1s 1920x1080, 14 shots, transcript unavailable",
   "job": "j_5a05b0c4b914",
   "files": [
    "hero-sheet.webp"
   ],
   "by": "hub"
  },
  {
   "id": 5,
   "from": "claude",
   "t": 8.82,
   "text": "14 shots in 1 min 30 s. Here's the sheet.",
   "job": null,
   "files": [
    "hero-sheet.webp"
   ],
   "by": "script"
  },
  {
   "id": 6,
   "from": "you",
   "t": 12.83,
   "text": "@claude cut 3 vertical clips",
   "job": null,
   "files": [],
   "by": "script"
  },
  {
   "id": 7,
   "from": "claude",
   "t": 12.84,
   "text": "job j_195f1426abd9 [clip] artifact=aa951457fe26a981e747213e5d581a84b8018cccdd27b148a2299bb941867680, count=3, seconds=10, aspect=9:16",
   "job": "j_195f1426abd9",
   "files": [],
   "by": "script"
  },
  {
   "id": 8,
   "from": "relay",
   "t": 13.01,
   "text": "@studio picked up job j_195f1426abd9 [clip]",
   "job": "j_195f1426abd9",
   "files": [],
   "by": "hub"
  },
  {
   "id": 9,
   "from": "relay",
   "t": 30.85,
   "text": "job j_195f1426abd9 [clip] done by @studio: 3 vertical clips (9:16, 10 s each) from 14 shots",
   "job": "j_195f1426abd9",
   "files": [
    "hero-clip1.mp4",
    "hero-clip1.jpg",
    "hero-clip2.mp4",
    "hero-clip2.jpg",
    "hero-clip3.mp4",
    "hero-clip3.jpg"
   ],
   "by": "hub"
  },
  {
   "id": 10,
   "from": "claude",
   "t": 31.03,
   "text": "Three vertical clips, 10 s each.",
   "job": null,
   "files": [
    "hero-clip1.mp4",
    "hero-clip2.mp4",
    "hero-clip3.mp4"
   ],
   "by": "script"
  },
  {
   "id": 11,
   "from": "muse",
   "t": 33.04,
   "text": "Post clip 2 at 9:00?",
   "job": null,
   "files": [],
   "by": "script"
  }
 ],
 "jobs": [
  {
   "id": "j_5a05b0c4b914",
   "kind": "digest",
   "by": "studio",
   "queued": 0.01,
   "done": 8.65,
   "seconds": 8.6,
   "summary": "90.1s 1920x1080, 14 shots, transcript unavailable"
  },
  {
   "id": "j_195f1426abd9",
   "kind": "clip",
   "by": "studio",
   "queued": 12.84,
   "done": 30.85,
   "seconds": 18.0,
   "summary": "3 vertical clips (9:16, 10 s each) from 14 shots"
  }
 ],
 "digest": {
  "shots": 14,
  "duration": 90.066,
  "sheet": "hero-sheet.webp",
  "sheet_size": [
   980,
   160
  ]
 },
 "clips": [
  {
   "file": "hero-clip1.mp4",
   "poster": "hero-clip1.jpg",
   "start": 52.133,
   "end": 62.133,
   "seconds": 10.0,
   "shot": 5,
   "activity": 1.56,
   "bytes": 1013436
  },
  {
   "file": "hero-clip2.mp4",
   "poster": "hero-clip2.jpg",
   "start": 65.099,
   "end": 75.099,
   "seconds": 10.0,
   "shot": 7,
   "activity": 2.981,
   "bytes": 741437
  },
  {
   "file": "hero-clip3.mp4",
   "poster": "hero-clip3.jpg",
   "start": 77.899,
   "end": 87.899,
   "seconds": 10.0,
   "shot": 11,
   "activity": 2.924,
   "bytes": 1301077
  }
 ],
 "clip_rule": "one window per shot, busiest (most frame-to-frame change) first, no overlaps, in film order",
 "clip_params": {
  "artifact": "(the source)",
  "count": 3,
  "seconds": 10,
  "aspect": "9:16"
 }
});
})();
