from datetime import datetime,timezone
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.models.entities import Job
from src.services.business import dump
def now():return datetime.now(timezone.utc).isoformat()
def save_job(s,data,i=None):
 if i:
  r=s.query(Job).filter_by(id=i).with_for_update().first()
  if not r:raise HTTPException(404,'维保作业不存在')
  # 人工修订显式推进版本，避免晚到的旧导入覆盖修订。
  r.code=data.code;r.equipment=data.equipment;r.parts=data.parts;r.note=data.note
  r.version=(r.version or 1)+1;r.source='manual';r.batch_no=None;r.updated_at=now()
 else:
  r=Job(**data.model_dump(),version=1,source='manual',batch_no=None,updated_at=now())
 s.add(r)
 try:s.commit()
 except IntegrityError:
  s.rollback();raise HTTPException(409,'作业编号重复')
 return dump(r)
