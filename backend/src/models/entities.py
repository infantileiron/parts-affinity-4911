from sqlalchemy import Column,Integer,String,Float,JSON,Text,ForeignKey,UniqueConstraint,Index
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True,nullable=False);password=Column(String(200));role=Column(String(20))
class Job(Base):
 __tablename__='jobs'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True,nullable=False);equipment=Column(String(40));parts=Column(JSON);note=Column(Text,default='')
 # 每张作业只有一个当前版本；既有作业从版本1接续，人工修订和较新导入都会推进版本。
 version=Column(Integer,nullable=False,default=1,server_default='1');source=Column(String(20),default='manual');batch_no=Column(String(64),nullable=True);updated_at=Column(String(40),default='')
class JobImportBatch(Base):
 __tablename__='job_import_batches'
 id=Column(Integer,primary_key=True);batch_no=Column(String(64),unique=True,nullable=False);content_hash=Column(String(64),nullable=False)
 state=Column(String(20),default='pending',nullable=False);actor=Column(String(100));total=Column(Integer,default=0)
 counts=Column(JSON,nullable=True);receipt=Column(JSON,nullable=True);error=Column(Text,default='')
 created=Column(String(40));updated=Column(String(40))
class JobImportRow(Base):
 __tablename__='job_import_rows'
 id=Column(Integer,primary_key=True);batch_id=Column(Integer,ForeignKey('job_import_batches.id'),nullable=False)
 line=Column(Integer,nullable=False);code=Column(String(60),nullable=True);version=Column(Integer,nullable=True)
 payload=Column(JSON,nullable=True);content_hash=Column(String(64),nullable=True)
 outcome=Column(String(20),default='pending',nullable=False);reason_code=Column(String(40),default='');reason=Column(Text,default='')
 job_id=Column(Integer,nullable=True);accepted_version=Column(Integer,nullable=True);current_version=Column(Integer,nullable=True)
 __table_args__=(UniqueConstraint('batch_id','line'),Index('ix_job_import_rows_batch_outcome','batch_id','outcome'))
class MiningRun(Base):
 __tablename__='mining_runs'
 id=Column(Integer,primary_key=True);name=Column(String(100));equipment=Column(String(40));min_support=Column(Float);min_confidence=Column(Float);min_lift=Column(Float);state=Column(String(20),default='queued');result=Column(JSON,nullable=True);error=Column(Text,default='')
class AssociationRule(Base):
 __tablename__='association_rules'
 id=Column(Integer,primary_key=True);run_id=Column(Integer,ForeignKey('mining_runs.id'));signature=Column(String(200));antecedent=Column(JSON);consequent=Column(JSON);support=Column(Float);confidence=Column(Float);lift=Column(Float);count=Column(Integer)
 __table_args__=(UniqueConstraint('run_id','signature'),)
class Kit(Base):
 __tablename__='kits'
 id=Column(Integer,primary_key=True);name=Column(String(100));rule_id=Column(Integer,ForeignKey('association_rules.id'));parts=Column(JSON);note=Column(Text);status=Column(String(20),default='draft')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))
