import os
import boto3
import psycopg2

def check_aws():
    sts = boto3.client("sts")
    sts.get_caller_identity()

def check_db():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        dbname=os.getenv("DB_NAME")
    )

    cur = conn.cursor()
    cur.execute("SELECT 1")
    cur.close()
    conn.close()

if __name__ == "__main__":
    check_aws()
    check_db()
    print("healthy")
