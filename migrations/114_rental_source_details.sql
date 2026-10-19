-- Genuine same-ID full-page observations only. No default, UPDATE or backfill.
-- description_checked_at remains the observation timestamp for this snapshot.
ALTER TABLE rental_listings ADD COLUMN IF NOT EXISTS source_details JSONB;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='rental_listings'::regclass
                   AND conname='rental_listings_source_details_check') THEN
        ALTER TABLE rental_listings ADD CONSTRAINT rental_listings_source_details_check
            CHECK (source_details IS NULL OR
                   (jsonb_typeof(source_details)='object' AND octet_length(source_details::text)<=16384));
    END IF;
END $$;
