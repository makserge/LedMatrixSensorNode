"""Replay harness of the radar zone learner (P3 spec Section 11).

The simulator's world as 10 Hz LD2454-like frames in P1 batches, a driver that feeds the learner core with
simulated time, ground truth, and the evaluation of the P3 pass criteria. Runs on this PC only; nothing
here is built into the app image (.dockerignore).
"""
