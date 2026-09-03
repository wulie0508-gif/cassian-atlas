CREATE TABLE subjects (
    subject_code TEXT PRIMARY KEY,
    name_en TEXT NOT NULL,
    name_cn TEXT NOT NULL,
    adapter_status TEXT NOT NULL DEFAULT 'generic' CHECK (adapter_status IN ('ready', 'generic')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL
);

INSERT INTO subjects(subject_code,name_en,name_cn,adapter_status,active,sort_order,created_at) VALUES
('english','English','英语','ready',1,10,CURRENT_TIMESTAMP),
('geography','Geography','地理','generic',1,20,CURRENT_TIMESTAMP),
('mathematics','Mathematics','数学','generic',1,30,CURRENT_TIMESTAMP),
('chinese','Chinese','语文','generic',1,40,CURRENT_TIMESTAMP),
('science','Science','科学','generic',1,50,CURRENT_TIMESTAMP);

CREATE TABLE student_subjects (
    student_id TEXT NOT NULL REFERENCES students(student_id),
    subject_code TEXT NOT NULL REFERENCES subjects(subject_code),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    enrolled_at TEXT NOT NULL,
    PRIMARY KEY(student_id, subject_code)
);

INSERT INTO student_subjects(student_id,subject_code,active,enrolled_at)
SELECT student_id,'english',1,CURRENT_TIMESTAMP FROM students;

ALTER TABLE content_items ADD COLUMN subject_code TEXT NOT NULL DEFAULT 'english';
CREATE INDEX idx_content_items_subject_domain ON content_items(subject_code,domain,record_status);
