import hashlib,json,re,time
from datetime import datetime,timezone
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError,OperationalError
from fastapi import HTTPException
from src.models.entities import Job,JobImportBatch,JobImportRow
from src.services.business import audit,dump
from src.constants.enums import EQUIPMENT,PARTS

# 批次在 processing 超过该秒数视为中断，可由同内容重试重新接管。
STALE_SECONDS=300
OUTCOMES=('pending','created','revised','expired','conflict','invalid')
OUTCOME_LABELS={'pending':'待处理','created':'新建','revised':'修订','expired':'过期','conflict':'同版本异文','invalid':'坏行'}
REASON_LABELS={
 'new_record':'首次出现，建立初始版本',
 'newer_version':'版本高于现有记录，已接纳',
 'same_version_identical':'同版本同内容，保留原记录',
 'older_version':'版本低于现有记录，只接纳较新版本',
 'same_version_divergent':'同版本但设备、用件或说明不同，已隔离',
 'not_object':'行内容不是对象，无法识别',
 'blank_line':'空行无法逐行对账，已隔离',
 'bad_field_count':'字段不足，需“作业号|版本|设备类型|备件|说明”',
 'missing_code':'作业号不能为空',
 'code_too_long':'作业号超过60字符',
 'bad_version':'版本号必须为不小于1的整数',
 'bad_equipment':'设备类型必须为泵组、压缩机或风机',
 'bad_parts':'备件集合必须非空且来自12项标准目录',
 'too_many_parts':'备件集合超过30项',
 'note_too_long':'维修说明超过1000字符',
 'unparseable':'行内容无法解析，已隔离',
 'concurrency_retry':'并发写入未能确定版本关系，请重试该批次',
}
def now():return datetime.now(timezone.utc).isoformat()

def _canon(v,key=None):
 if isinstance(v,dict):return {str(k):_canon(v[k],k) for k in sorted(v)}
 # 备件是集合，行内顺序不影响业务内容，哈希前统一排序。
 if isinstance(v,list):
  return sorted((_canon(x) for x in v),key=lambda x:json.dumps(x,sort_keys=True,ensure_ascii=False)) if key=='parts' else [_canon(x) for x in v]
 if isinstance(v,(str,int,float,bool)) or v is None:return v
 return str(v)
def _content_row(item):
 """批内容按业务载荷归一：原始行与结构化行只要内容相同，视为同一批内容。"""
 payload,error=normalize(item)
 if payload is not None:return payload
 return {'invalid':_stored_payload(item)}
def content_hash(rows):
 canonical=[_content_row(item) for item in rows]
 return hashlib.sha256(json.dumps(_canon(canonical),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def row_hash(row):return hashlib.sha256(json.dumps(_canon(row),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def _parse_raw(raw):
 text=str(raw).strip()
 if not text:return None,'blank_line'
 # 支持从表格粘贴：优先竖线，其次制表符。
 fields=text.split('|') if '|' in text else text.split('\t')
 if len(fields)<4:return None,'bad_field_count'
 if len(fields)>5:fields=fields[:4]+['|'.join(fields[4:])]
 if len(fields)==4:fields.append('')
 return {'code':fields[0],'version':fields[1],'equipment':fields[2],'parts':fields[3],'note':fields[4]},None

def normalize(item):
 """把提交行还原为规范载荷；坏行返回 (None, 原因代码)。"""
 if isinstance(item,str):
  item={'raw':item}
 if not isinstance(item,dict):return None,'not_object'
 if isinstance(item.get('raw'),str) and not any(k in item for k in ('code','version')):
  item,error=_parse_raw(item['raw'])
  if error:return None,error
 try:
  code=str(item.get('code') or '').strip()
  version_raw=item.get('version')
  equipment=str(item.get('equipment') or '').strip()
  parts_raw=item.get('parts')
  note=str(item.get('note') if item.get('note') is not None else '').strip()
  if not code:return None,'missing_code'
  if len(code)>60:return None,'code_too_long'
  if isinstance(version_raw,bool) or version_raw is None:return None,'bad_version'
  if isinstance(version_raw,float) and not version_raw.is_integer():return None,'bad_version'
  try:version=int(str(version_raw).strip())
  except (TypeError,ValueError):return None,'bad_version'
  if version<1:return None,'bad_version'
  if equipment not in EQUIPMENT:return None,'bad_equipment'
  if isinstance(parts_raw,str):parts=re.split(r'[,，、/;；\s]+',parts_raw)
  elif isinstance(parts_raw,list):parts=[str(p) for p in parts_raw]
  else:parts=[]
  parts=sorted(set(p.strip() for p in parts if str(p).strip()))
  if not parts:return None,'bad_parts'
  if len(parts)>30:return None,'too_many_parts'
  if any(p not in PARTS for p in parts):return None,'bad_parts'
  if len(note)>1000:return None,'note_too_long'
  return {'code':code,'version':version,'equipment':equipment,'parts':parts,'note':note},None
 except Exception:return None,'unparseable'

def _stored_payload(item):
 if isinstance(item,str):return {'raw':item}
 if isinstance(item,dict) and isinstance(item.get('raw'),str) and not any(k in item for k in ('code','version')):
  return {'raw':item['raw']}
 return _canon(item)

def _raw_fields(payload):
 if isinstance(payload,dict):
  if isinstance(payload.get('raw'),str):text=payload['raw']
  else:return []
 else:text=str(payload or '')
 text=text.strip()
 return text.split('|') if '|' in text else text.split('\t')

def _best_code(payload):
 try:
  fields=_raw_fields(payload);code=fields[0].strip() if fields else ''
  return code[:60] or None
 except Exception:return None

def _best_version(payload):
 try:
  fields=_raw_fields(payload)
  return int(fields[1].strip()) if len(fields)>1 and fields[1].strip().isdigit() else None
 except Exception:return None

def _same(job,payload):
 return (job.equipment==payload['equipment'] and sorted(job.parts or [])==payload['parts']
         and (job.note or '')==payload['note'])

def _apply(s,payload,batch_no):
 code=payload['code'];version=payload['version'];stamp=now()
 for _ in range(6):
  # 条件更新保证并发导入同一作业时只有最高版本胜出，版本号绝不回退。
  result=s.execute(update(Job).where(Job.code==code,Job.version<version).values(
   equipment=payload['equipment'],parts=payload['parts'],note=payload['note'],version=version,
   source='import',batch_no=batch_no,updated_at=stamp))
  if result.rowcount:
   job=s.query(Job).filter_by(code=code).populate_existing().one()
   return 'revised','newer_version',job
  job=s.query(Job).filter_by(code=code).with_for_update().populate_existing().first()
  if job is None:
   try:
    with s.begin_nested():
     job=Job(code=code,equipment=payload['equipment'],parts=payload['parts'],note=payload['note'],
             version=version,source='import',batch_no=batch_no,updated_at=stamp)
     s.add(job);s.flush()
    return 'created','new_record',job
   except IntegrityError:
    s.expire_all();continue
  if job.version<version:continue
  if job.version==version:
   return ('expired','same_version_identical',job) if _same(job,payload) else ('conflict','same_version_divergent',job)
  return 'expired','older_version',job
 return 'conflict','concurrency_retry',None

def _counts(rows):
 out={k:0 for k in OUTCOMES}
 for r in rows:out[r.outcome]=out.get(r.outcome,0)+1
 out['total']=len(rows);out['isolated']=out['conflict']+out['invalid']
 return out

def _receipt(batch,rows,counts):
 return {'batch_no':batch.batch_no,'batch_id':batch.id,'state':batch.state,'actor':batch.actor,
         'content_hash':batch.content_hash,'total':len(rows),'counts':counts,
         'lines':[{'line':r.line,'code':r.code,'submitted_version':r.version,'outcome':r.outcome,
                   'outcome_label':OUTCOME_LABELS.get(r.outcome,r.outcome),
                   'reason_code':r.reason_code,'reason':REASON_LABELS.get(r.reason_code,r.reason or r.reason_code),
                   'job_id':r.job_id,'accepted_version':r.accepted_version,'current_version':r.current_version}
                  for r in rows],
         'started_at':batch.created,'finished_at':batch.updated}

def _summary(s,batch):
 rows=s.query(JobImportRow).filter_by(batch_id=batch.id).order_by(JobImportRow.line).all()
 return {'batch':dump(batch),'rows':[dump(r) for r in rows],'receipt':batch.receipt,
         'progress':{'total':len(rows),'pending':sum(1 for r in rows if r.outcome=='pending'),
                     'isolated':sum(1 for r in rows if r.outcome in ('conflict','invalid'))}}

def _claim(s,batch):
 result=s.execute(update(JobImportBatch).where(JobImportBatch.id==batch.id,JobImportBatch.state=='pending')
                  .values(state='processing',updated=now()))
 s.commit()
 return bool(result.rowcount)

def _wait_done(s,batch,timeout=10.0):
 """另一个请求正在处理同批内容时，短暂等待其完成以便返回同一份回执。"""
 deadline=time.monotonic()+timeout
 while time.monotonic()<deadline:
  # 结束当前事务再读取，避免 MySQL REPEATABLE READ 快照看不到接管者的提交。
  s.rollback()
  current=s.query(JobImportBatch).filter_by(id=batch.id).first()
  if current is None or current.state in ('done','failed'):return current
  time.sleep(.05)
 s.rollback()
 return s.query(JobImportBatch).filter_by(id=batch.id).populate_existing().first()

def _reclaim_stale(s,batch):
 if batch.state!='processing' or not batch.updated:return False
 try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(batch.updated)).total_seconds()
 except ValueError:return False
 if age<STALE_SECONDS:return False
 stale_before=batch.updated
 result=s.execute(update(JobImportBatch).where(JobImportBatch.id==batch.id,JobImportBatch.state=='processing',
                  JobImportBatch.updated==stale_before).values(state='pending',updated=now()))
 s.commit()
 return bool(result.rowcount)

def _process(s,batch):
 rows=s.query(JobImportRow).filter_by(batch_id=batch.id).order_by(JobImportRow.line).all()
 # 以作业号稳定排序后写入，使并发批次对同一作业的加锁顺序一致，避免交叉死锁。
 for row in sorted(rows,key=lambda r:(str(r.code or _best_code(r.payload) or ''),r.line)):
  if row.outcome!='pending':continue
  payload,error=normalize(row.payload)
  if error:
   row.outcome='invalid';row.reason_code=error;row.reason=REASON_LABELS.get(error,error)
   row.code=row.code or _best_code(row.payload)
   row.version=row.version or _best_version(row.payload)
   row.job_id=None;row.accepted_version=None;row.current_version=None
   continue
  row.code=payload['code'];row.version=payload['version']
  outcome,reason,job=_apply(s,payload,batch.batch_no)
  row.outcome=outcome;row.reason_code=reason;row.reason=REASON_LABELS.get(reason,reason)
  row.job_id=job.id if job else None
  row.accepted_version=job.version if job and outcome in ('created','revised') else None
  row.current_version=job.version if job else None
  s.flush()
 counts=_counts(rows)
 batch.counts=counts;batch.state='done';batch.error='';batch.updated=now()
 batch.total=len(rows)
 batch.receipt=_receipt(batch,rows,counts)
 s.commit()
 return _summary(s,batch),counts

def _fail(s,batch,message):
 try:
  s.rollback()
  batch=s.query(JobImportBatch).filter_by(id=batch.id).populate_existing().one()
  counts=_counts(s.query(JobImportRow).filter_by(batch_id=batch.id).all())
  batch.state='failed';batch.error=message[:500];batch.counts=counts;batch.updated=now()
  s.commit()
 except Exception:s.rollback()

def submit(s,data,user):
 batch_no=data.batch_no.strip();rows=list(data.rows);digest=content_hash(rows)
 existing=s.query(JobImportBatch).filter_by(batch_no=batch_no).first()
 if existing:
  if existing.content_hash!=digest:
   raise HTTPException(409,f'批号 {batch_no} 已存在且本次内容不同，请更换批号后重试')
  # 同批号同内容重试：已完成批次直接返回原回执，不重复写入。
  if existing.state=='done':
   return _summary(s,existing),True
  # 仅预览（commit=false）时不接管、不处理，也没有可重放的回执。
  if not data.commit:return _summary(s,existing),False
  if existing.state=='failed':
   s.execute(update(JobImportBatch).where(JobImportBatch.id==existing.id,JobImportBatch.state=='failed')
             .values(state='pending',error='',updated=now()))
   s.query(JobImportRow).filter_by(batch_id=existing.id).update(
    {'outcome':'pending','reason_code':'','reason':'','job_id':None,'accepted_version':None,'current_version':None},
    synchronize_session=False)
   s.commit();s.refresh(existing)
  elif existing.state=='processing':
   if not _reclaim_stale(s,existing):
    # 并发重试：等接管者完成后返回同一份回执，而不是空进度。
    done=_wait_done(s,existing)
    return _summary(s,done or existing),True
   s.refresh(existing)
  if existing.state!='pending':return _summary(s,existing),True
  if not _claim(s,existing):
   done=_wait_done(s,existing)
   return _summary(s,done or existing),True
  return _finish(s,user,existing)
 # 新批次：先落批次和全部行为待处理，再接续处理，保证逐行对账。
 batch=JobImportBatch(batch_no=batch_no,content_hash=digest,state='pending',actor=user.email,
                      total=len(rows),created=now(),updated=now())
 s.add(batch)
 try:s.flush()
 except IntegrityError:
  s.rollback()
  return submit(s,data,user)
 for index,item in enumerate(rows,1):
  s.add(JobImportRow(batch_id=batch.id,line=index,payload=_stored_payload(item),content_hash=row_hash(item),
                     outcome='pending'))
 s.commit()
 if not data.commit:return _summary(s,batch),False
 if not _claim(s,batch):
  done=_wait_done(s,batch)
  return _summary(s,done or batch),True
 return _finish(s,user,batch)

def _finish(s,user,batch):
 for attempt in range(3):
  try:detail,counts=_process(s,batch)
  except OperationalError:
   s.rollback()
   if attempt==2:
    _fail(s,batch,'数据库并发冲突，批次已标记失败，可用相同内容重试')
    raise HTTPException(503,'数据库并发冲突，批次已标记失败，可用相同内容重试')
   continue
  except Exception as error:
   _fail(s,batch,str(error))
   raise HTTPException(500,'导入过程异常，批次已标记失败，可用相同内容重试')
  _audit(s,user,detail['batch'],counts)
  return detail,False
 return _summary(s,batch),False

def _audit(s,user,batch,counts):
 audit(s,user,f'批量导入作业 {batch["batch_no"]}：新建{counts["created"]} 修订{counts["revised"]} '
              f'过期{counts["expired"]} 同版本异文{counts["conflict"]} 坏行{counts["invalid"]}')

def list_batches(s,limit=30):
 out=[]
 for r in s.query(JobImportBatch).order_by(JobImportBatch.id.desc()).limit(limit).all():
  item=dump(r);item.pop('receipt',None);out.append(item)
 return out
def detail(s,batch_no):
 batch=s.query(JobImportBatch).filter_by(batch_no=batch_no).first()
 if not batch:raise HTTPException(404,'导入批次不存在')
 return _summary(s,batch)
def reconcile(s,batch_no):
 """逐行对账：提交行数是否等于分类之和，并标出已被后续版本推进的作业。"""
 data=detail(s,batch_no);rows=data['rows']
 current={j.code:j.version for j in s.query(Job).all()}
 lines=[]
 for row in rows:
  now_version=current.get(row['code']) if row['code'] else None
  lines.append({'line':row['line'],'code':row['code'],'submitted_version':row['version'],
                'outcome':row['outcome'],'reason_code':row['reason_code'],
                'accepted_version':row['accepted_version'],'current_version':now_version,
                'superseded_later':bool(row['outcome'] in ('created','revised') and now_version is not None
                                        and now_version>(row['accepted_version'] or 0))})
 counted=sum(1 for r in rows if r['outcome'] in OUTCOMES)
 balanced=counted==len(rows) and data['progress']['pending']==0
 return {**data,'reconcile':{'balanced':balanced,'submitted':len(rows),'classified':counted,
                             'pending':data['progress']['pending'],'superseded_later':sum(1 for l in lines if l['superseded_later']),
                             'lines':lines}}
def labels():return {'outcomes':dict(OUTCOME_LABELS),'reasons':dict(REASON_LABELS),'stale_seconds':STALE_SECONDS}
