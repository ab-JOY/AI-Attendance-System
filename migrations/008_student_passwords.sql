-- =====================================================
-- 008 - Student passwords for mobile app authentication
--
-- The mobile app needs students to log in. The web app never had student
-- authentication — students were managed by admins and stood in front of a
-- server-side camera. The mobile app is different: a student opens the app,
-- logs in, and presents their face to their own phone camera.
--
-- NULL means "no password set" — existing students enrolled before this
-- migration cannot log in via mobile until an admin sets a password for them
-- or they register through the mobile app.
-- =====================================================

ALTER TABLE students ADD COLUMN password VARCHAR(255) DEFAULT NULL;
