from typing import Optional, List, Dict
from sqlalchemy import create_engine, text
from pydantic_settings import BaseSettings, SettingsConfigDict
import pandas as pd
import logging
import math

log = logging.getLogger(__name__)

class DBSettings(BaseSettings):
    database_url: str
    student_name: str

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore"
    )

settings = DBSettings()
engine = create_engine(settings.database_url)

SCHEMA = "ai_engineer3"
ARTICLES_TABLE = f'"{SCHEMA}".articles_{settings.student_name}'
ATTRITION_TABLE_NAME = f"raw_attrition_{settings.student_name}"
ATTRITION_TABLE = f'"{SCHEMA}".{ATTRITION_TABLE_NAME}'
PRODUCTS_TABLE = f'"{SCHEMA}".products'

def init_db():
    """Create tables and indexes if they don't exist."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {ARTICLES_TABLE} (
        id           SERIAL PRIMARY KEY,
        title        TEXT NOT NULL,
        url          TEXT UNIQUE NOT NULL,
        source       TEXT NOT NULL,
        content      TEXT,
        published_at TIMESTAMP,
        scraped_at   TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_articles_source
        ON {ARTICLES_TABLE}(source);
    CREATE INDEX IF NOT EXISTS idx_articles_published
        ON {ARTICLES_TABLE}(published_at DESC);
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))
    log.info("Database ready. Table: %s", ARTICLES_TABLE)

def save_articles(df: pd.DataFrame) -> int:
    """Insert articles with ON CONFLICT — safe to re-run."""
    if df.empty:
        log.warning("DataFrame kosong, tidak ada yang di-insert.")
        return 0

    inserted = 0
    with engine.begin() as conn:
        for _, row in df.iterrows():
            result = conn.execute(
                text(f"""
                    INSERT INTO {ARTICLES_TABLE}
                        (title, url, source, content, published_at)
                    VALUES
                        (:title, :url, :source, :content, :published_at)
                    ON CONFLICT (url) DO NOTHING
                """),
                {
                    "title": row["title"],
                    "url": row["url"],
                    "source": row["source"],
                    "content": row["content"],
                    "published_at": row["published_at"]
                    if pd.notna(row["published_at"])
                    else None,
                },
            )
            inserted += result.rowcount

    log.info("Insert selesai: %d baru dari %d record.", inserted, len(df))
    return inserted


def count_articles() -> int:
    """Count total articles in DB."""
    with engine.connect() as conn:
        result = conn.execute(text(f"SELECT COUNT(*) FROM {ARTICLES_TABLE}"))
        return result.scalar()

def count_articles_by_source() -> Dict[str, int]:
    """
    Hitung jumlah artikel per source.
    Return contoh: {"bbc": 50, "nytimes": 30, "books.toscrape": 200}
    """
    # TODO 18: Tulis query SELECT source, COUNT(*) ... GROUP BY source
    #          Lalu convert hasil ke dictionary {source: count}
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT source, COUNT(*) as cnt FROM {ARTICLES_TABLE}"
            f" GROUP BY source ORDER BY cnt DESC"
        )).mappings().all()
    return {row["source"]: row["cnt"] for row in rows}

def get_articles(
    source: Optional[str] = None,
    title: Optional[str] = None,
    limit: int = 20,
) -> List[Dict]:
    """
    Cari articles dengan optional filter source dan title.

    Contoh pemanggilan:
      get_articles(source="bbc", limit=5)           → artikel BBC saja
      get_articles(title="python", limit=10)         → judul mengandung "python"
      get_articles(source="bbc", title="AI", limit=5) → BBC + judul ada "AI"
    """
    # TODO 19: Bangun query SELECT secara dinamis berdasarkan parameter
    #
    # Hint:
    #   - Mulai dengan: query = f"SELECT * FROM {ARTICLES_TABLE} WHERE 1=1"
    #   - Jika source ada:   tambah " AND source = :source"
    #   - Jika title ada:    tambah " AND title ILIKE :title"
    #     (ILIKE = case-insensitive LIKE di PostgreSQL)
    #     params["title"] = f"%{title}%"   ← wildcard di kedua sisi
    #   - Akhiri dengan: ORDER BY published_at DESC NULLS LAST LIMIT :limit
    #   - Execute dengan parameterized query (JANGAN string format untuk values!)
    #   - Return list of dicts: [dict(r) for r in rows]
    query = f"SELECT * FROM {ARTICLES_TABLE} WHERE 1=1"
    params: dict = {}

    if source:
        query += " AND source = :source"
        params["source"] = source.lower()

    if title:
        query += " AND title ILIKE :title"
        params["title"] = f"%{title}%"

    query += " ORDER BY published_at DESC NULLS LAST LIMIT :limit"
    params["limit"] = limit

    with engine.connect() as conn:
        rows = conn.execute(text(query), params).mappings().all()
    return [dict(r) for r in rows]

def get_article_by_id(article_id: int) -> Optional[Dict]:
    """Get single article by ID. Return None jika tidak ditemukan."""
    # TODO 20: SELECT * FROM ... WHERE id = :id
    #
    # Hint:
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT * FROM {ARTICLES_TABLE} WHERE id = :id"),
            {"id": article_id},
        ).mappings().first()
    return dict(row) if row else None

def get_article_search(
    source: Optional[str] = None,
    title: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    per_page: int = 10,
) -> Dict:
    """Search articles dengan filter dinamis dan pagination."""
    conditions = []
    params = {}

    if source:
        conditions.append("LOWER(source) = LOWER(:source)")
        params["source"] = source

    if title:
        # Case-insensitive partial match pada judul[cite: 7]
        conditions.append("LOWER(title) LIKE LOWER(:title)")
        params["title"] = f"%{title}%"

    if date_from:
        # Filter published_at >= YYYY-MM-DD[cite: 7]
        conditions.append("DATE(published_at) >= :date_from")
        params["date_from"] = date_from

    if date_to:
        # Filter published_at <= YYYY-MM-DD[cite: 7]
        conditions.append("DATE(published_at) <= :date_to")
        params["date_to"] = date_to

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with engine.connect() as conn:
        # 1. Hitung total item yang sesuai filter
        count_sql = text(f"SELECT COUNT(*) FROM {ARTICLES_TABLE} {where_clause}")
        total_items = conn.execute(count_sql, params).scalar() or 0

        # 2. Hitung total_pages dan OFFSET[cite: 7]
        total_pages = math.ceil(total_items / per_page) if total_items > 0 else 0
        offset = (page - 1) * per_page

        # 3. Ambil data artikel sesuai halaman (LIMIT & OFFSET)[cite: 7]
        data_sql = text(f"""
            SELECT * FROM {ARTICLES_TABLE}
            {where_clause}
            ORDER BY published_at DESC
            LIMIT :limit OFFSET :offset
        """)
        query_params = {**params, "limit": per_page, "offset": offset}
        rows = conn.execute(data_sql, query_params).mappings().all()

    return {
        "data": [dict(row) for row in rows],
        "page": page,
        "per_page": per_page,
        "total_items": total_items,
        "total_pages": total_pages,
    }

def get_products(category: Optional[str] = None, limit: int = 5) -> List[Dict]:
    """Get list of products dengan optional filter category."""
    with engine.connect() as conn:
        query = f"SELECT * FROM {PRODUCTS_TABLE}"
        params = {"limit": limit}
        
        # Tambahkan filter category jika diisi oleh user
        if category:
            query += " WHERE category = :category"
            params["category"] = category
            
        query += " ORDER BY product_id DESC LIMIT :limit"
        
        rows = conn.execute(text(query), params).mappings().all()
    return [dict(r) for r in rows]

def get_department_detail(dept_name: str) -> Optional[Dict]:
    """Ambil statistik lengkap dan top 3 earners untuk suatu department."""
    with engine.connect() as conn:
        # 1. Query agregasi statistik department (case-insensitive pada nama department)[cite: 10]
        stats_sql = text(f"""
            SELECT
                MAX("Department") AS department,
                COUNT(*) AS total_employees,
                SUM(CASE WHEN "Attrition" = 'Yes' THEN 1 ELSE 0 END) AS attrition_count,
                AVG("MonthlyIncome") AS avg_income,
                MIN("MonthlyIncome") AS min_income,
                MAX("MonthlyIncome") AS max_income
            FROM {ATTRITION_TABLE}
            WHERE LOWER("Department") = LOWER(:dept_name)
        """)
        stats_row = (
            conn.execute(stats_sql, {"dept_name": dept_name}).mappings().first()
        )

        # Jika department tidak ditemukan (total_employees == 0), kembalikan None[cite: 10]
        if not stats_row or stats_row["total_employees"] == 0:
            return None

        # 2. Query top 3 earners menggunakan RANK() OVER (ORDER BY "MonthlyIncome" DESC)[cite: 10]
        top_earners_sql = text(f"""
            SELECT
                "EmployeeNumber",
                "JobRole",
                "MonthlyIncome"
            FROM (
                SELECT
                    "EmployeeNumber",
                    "JobRole",
                    "MonthlyIncome",
                    RANK() OVER (ORDER BY "MonthlyIncome" DESC) AS rnk
                FROM {ATTRITION_TABLE}
                WHERE LOWER("Department") = LOWER(:dept_name)
            ) ranked
            ORDER BY rnk ASC
            LIMIT 3
        """)
        top_rows = (
            conn.execute(top_earners_sql, {"dept_name": dept_name})
            .mappings()
            .all()
        )

    total_emp = stats_row["total_employees"]
    attrition_cnt = stats_row["attrition_count"] or 0
    attrition_rate = round(attrition_cnt / total_emp, 4) if total_emp > 0 else 0.0
    avg_inc = (
        round(float(stats_row["avg_income"]), 2)
        if stats_row["avg_income"] is not None
        else 0.0
    )

    return {
        "department": stats_row["department"],
        "total_employees": total_emp,
        "attrition_count": attrition_cnt,
        "attrition_rate": attrition_rate,
        "avg_income": avg_inc,
        "min_income": stats_row["min_income"],
        "max_income": stats_row["max_income"],
        "top_earners": [dict(row) for row in top_rows],
    }    

def get_attrition_risk_profile(limit: int = 20) -> List[Dict]:
    """Ambil daftar karyawan dengan indikator risiko attrition tinggi[cite: 11]."""
    query = text(f"""
        WITH dept_avg AS (
            SELECT
                "Department",
                AVG("MonthlyIncome") AS avg_income
            FROM {ATTRITION_TABLE}
            GROUP BY "Department"
        )
        SELECT e.*
        FROM {ATTRITION_TABLE} e
        JOIN dept_avg d ON e."Department" = d."Department"
        WHERE e."OverTime" = 'Yes'
          AND e."YearsAtCompany" < 3
          AND e."MonthlyIncome" < d.avg_income
        LIMIT :limit
    """)

    with engine.connect() as conn:
        rows = conn.execute(query, {"limit": limit}).mappings().all()

    return [dict(row) for row in rows]

def get_attrition_summary() -> Dict:
    """
    Overall stats: total karyawan, jumlah & rate attrition.
    Return contoh: {"total_employees": 1470, "attrition_yes": 237, ...}
    """
    # TODO 21: Hitung total karyawan dan jumlah yang Attrition = 'Yes'
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT 
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE "Attrition" = 'Yes') AS attrition_yes
            FROM {ATTRITION_TABLE}
        """)).mappings().one()
        total = row["total"]
        attrition_yes = row["attrition_yes"]
    return {
        "total_employees": total,
        "attrition_yes": attrition_yes,
        "attrition_no": total - attrition_yes,
        "attrition_rate": round(attrition_yes / total, 4) if total > 0 else 0.0,
    }

def get_attrition_by_department() -> List[Dict]:
    """
    Attrition rate + avg income per department.
    Ini gabungan query 1 & 2 dari transform_attrition.sql Session 9.
    """
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT
                "Department",
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE "Attrition" = 'Yes') AS attrition_count,
                ROUND(
                    COUNT(*) FILTER (WHERE "Attrition" = 'Yes')::numeric / COUNT(*), 3
                ) AS attrition_rate,
                ROUND(AVG("MonthlyIncome")::numeric, 2) AS avg_income
            FROM {ATTRITION_TABLE}
            GROUP BY "Department"
            ORDER BY attrition_rate DESC
        """)).mappings().all()
    return [dict(r) for r in rows]

def get_attrition_by_tenure() -> List[Dict]:
    """
    Attrition by tenure bucket.
    Ini query 3 dari transform_attrition.sql Session 9.
    """
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT
                CASE
                    WHEN "YearsAtCompany" < 2 THEN '1. new (<2y)'
                    WHEN "YearsAtCompany" < 5 THEN '2. mid (2-5y)'
                    ELSE '3. veteran (5y+)'
                END AS tenure_bucket,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE "Attrition" = 'Yes') AS attrition_count,
                ROUND(
                    COUNT(*) FILTER (WHERE "Attrition" = 'Yes')::numeric / COUNT(*), 3
                ) AS attrition_rate
            FROM {ATTRITION_TABLE}
            GROUP BY tenure_bucket
            ORDER BY tenure_bucket
        """)).mappings().all()
    return [dict(r) for r in rows]


def get_attrition_by_overtime() -> List[Dict]:
    """
    Attrition by overtime status.
    Ini query 4 dari transform_attrition.sql Session 9.
    """
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT
                "OverTime",
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE "Attrition" = 'Yes') AS attrition_count,
                ROUND(
                    COUNT(*) FILTER (WHERE "Attrition" = 'Yes')::numeric / COUNT(*), 3
                ) AS attrition_rate
            FROM {ATTRITION_TABLE}
            GROUP BY "OverTime"
            ORDER BY "OverTime"
        """)).mappings().all()
    return [dict(r) for r in rows]


def get_top_earners_by_department(limit_per_dept: int = 5) -> List[Dict]:
    """
    Top earners per department menggunakan RANK() window function.
    Ini query 5 dari transform_attrition.sql Session 9.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT *
                FROM (
                    SELECT
                        "EmployeeNumber",
                        "Department",
                        "JobRole",
                        "MonthlyIncome",
                        "Attrition",
                        RANK() OVER (
                            PARTITION BY "Department"
                            ORDER BY "MonthlyIncome" DESC
                        ) AS income_rank
                    FROM {ATTRITION_TABLE}
                ) ranked
                WHERE income_rank <= :limit_per_dept
                ORDER BY "Department", income_rank
            """),
            {"limit_per_dept": limit_per_dept},
        ).mappings().all()
    return [dict(r) for r in rows]