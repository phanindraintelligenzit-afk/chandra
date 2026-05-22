provider "aws" {
  region = "us-east-1" # Update this to match your bucket's actual region
}

resource "aws_s3_bucket_public_access_block" "secure_bucket" {
  bucket = "chandra-synth-electric-gelding-leaky"

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}