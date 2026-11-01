-- 059: public holidays. A holiday is not an absent day: nobody is marked absent for it (no salary is cut), it does not count in the
-- attendance percentages, and it breaks a "sandwich". Someone who does come in on a holiday is shown as they checked in.
-- The owner adds or removes holidays on the Attendance owner dashboard > Employees.

CREATE TABLE att_holiday (
    holiday_date date PRIMARY KEY,
    holiday_name varchar(80) NOT NULL,
    created_by_user_key integer REFERENCES dim_user (user_key),
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO att_holiday (holiday_date, holiday_name) VALUES
    ('2026-01-26', 'Republic Day'), ('2026-08-15', 'Independence Day'), ('2026-10-02', 'Gandhi Jayanti'),
    ('2027-01-26', 'Republic Day'), ('2027-08-15', 'Independence Day'), ('2027-10-02', 'Gandhi Jayanti')
ON CONFLICT DO NOTHING;
