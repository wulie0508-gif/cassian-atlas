ALTER TABLE students ADD COLUMN grade_level TEXT;
ALTER TABLE students ADD COLUMN exam_system TEXT;
ALTER TABLE students ADD COLUMN target_exam_date TEXT;
ALTER TABLE students ADD COLUMN target_score REAL;
ALTER TABLE students ADD COLUMN weekly_hours REAL;
ALTER TABLE students ADD COLUMN course_stage TEXT;
ALTER TABLE students ADD COLUMN teacher_notes TEXT;

CREATE INDEX idx_student_subjects_active
ON student_subjects(student_id, active, subject_code);
