#!/usr/bin/env python3
"""Offline, private manual-source furnishing audit; no database or HTTP calls.

Create an empty schema with --template. A human then visits actual source
pages, saves screenshots privately and labels at least 50 different ads before
running --input FILE --expected-dataset-sha SHA. Synthetic cases are useful
unit tests and can never produce an eligible validation receipt.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from bot.core.rental_furnishing import VERSION,LABELS,classify_furnishing,compile_custom_rules,rules_sha256
from scripts.rental_stage0_maintenance import private_json,validate_output


def dataset_template():
    return {'schema_version':1,'dataset_kind':'manual_real_source','status':'external_validation_pending',
            'instructions':'Visit and independently label at least 50 actual Krisha ads. Do not generate examples here.',
            'record_schema':{'listing_id':'digits','source_url':'https://krisha.kz/a/show/ID',
                'checked_at':'ISO timezone date','title':'observed title','description':'observed description',
                'text_sha256':'SHA256 of title + newline + description','label':'none|partial|full|unknown',
                'label_source':'human_blind','labeler':'private reviewer ID','receipt_path':'relative path to actual browser screenshot',
                'receipt_sha256':'SHA256 of screenshot bytes'},'records':[]}


def evaluate_dataset(dataset,*,dataset_sha256,rules='{}',now=None,receipt_check=None):
    now=now or datetime.now(timezone.utc)
    errors=[]
    matrix={label:{other:0 for other in sorted(LABELS)} for label in sorted(LABELS)}
    seen=set()
    receipts=set()
    valid=[]
    records=dataset.get('records') if isinstance(dataset,dict) else None
    if not isinstance(records,list) or len(records)>1000:
        records=[]
        errors.append('invalid_records')
    custom=compile_custom_rules(rules)
    for index,row in enumerate(records):
        try:
            lid=row['listing_id']
            checked=datetime.fromisoformat(row['checked_at'].replace('Z','+00:00'))
            text=row['title']+'\n'+row['description']
            good=(isinstance(lid,str) and re.fullmatch(r'\d{5,20}',lid) and lid not in seen
                  and row['source_url']==f'https://krisha.kz/a/show/{lid}'
                  and checked.tzinfo is not None and now-timedelta(days=90)<=checked<=now
                  and row['label'] in LABELS and bool(row.get('labeler')) and row.get('label_source')=='human_blind'
                  and isinstance(row.get('receipt_sha256'),str) and re.fullmatch(r'[0-9a-f]{64}',row['receipt_sha256'])
                  and row['receipt_sha256'] not in receipts
                  and len(text)<=20000 and hashlib.sha256(text.encode()).hexdigest()==row['text_sha256']
                  and receipt_check is not None and receipt_check(row))
        except (ValueError,TypeError,KeyError,AttributeError):
            good=False
        if not good:
            errors.append(f'record_{index+1}_not_verified')
            continue
        seen.add(lid)
        receipts.add(row['receipt_sha256'])
        guess=classify_furnishing(row['title'],row['description'],custom)
        matrix[row['label']][guess]+=1
        valid.append(row)
    count=len(valid)
    accuracy=sum(matrix[label][label] for label in LABELS)/count if count else 0
    support={label:sum(matrix[label].values()) for label in LABELS}
    minimum=min(support[label] for label in ('none','partial','full'))
    real=isinstance(dataset,dict) and dataset.get('schema_version')==1 and dataset.get('dataset_kind')=='manual_real_source'
    eligible=bool(real and not errors and count>=50 and minimum>=5 and accuracy>=.9)
    report={'schema_version':1,'dataset_kind':dataset.get('dataset_kind') if isinstance(dataset,dict) else None,
            'classifier_version':VERSION,'dataset_sha256':dataset_sha256,'rules_sha256':rules_sha256(rules),
            'reviewed_at':now.isoformat(),'unique_reviewed':count,'accuracy':round(accuracy,6),
            'per_label_support':support,'known_labels_min_support':minimum,'confusion_matrix':matrix,
            'eligible_for_operator_enable':eligible,'errors':errors,
            'status':'ready_for_operator_review' if eligible else 'external_validation_pending'}
    return report


def screenshot_check(root,row):
    try:
        path=(root/row['receipt_path']).resolve()
        path.relative_to(root.resolve())
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):
            return False
        if not 100<=path.stat().st_size<=20_000_000:
            return False
        raw=path.read_bytes()
        image=raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8') or raw[:4]==b'RIFF' and raw[8:12]==b'WEBP'
        return bool(image and hashlib.sha256(raw).hexdigest()==row['receipt_sha256'])
    except (ValueError,TypeError,KeyError,OSError):
        return False


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template',action='store_true')
    parser.add_argument('--input',type=Path)
    parser.add_argument('--expected-dataset-sha')
    parser.add_argument('--rules-file',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        validate_output(args.output)
        if args.template:
            if args.input or args.expected_dataset_sha:
                raise ValueError('Template does not accept source input')
            private_json(args.output,dataset_template())
        else:
            if not args.input or not re.fullmatch(r'[0-9a-f]{64}',args.expected_dataset_sha or ''):
                raise ValueError('An explicitly reviewed input SHA is required')
            if args.input.resolve()==args.output.resolve() or args.input.is_symlink() or args.input.stat().st_size>8_000_000:
                raise ValueError('Keep the bounded private input and report separate')
            validate_output(args.input)
            if args.rules_file and args.rules_file.resolve()==args.output.resolve():
                raise ValueError('Output must not replace the reviewed rules')
            raw=args.input.read_bytes()
            actual=hashlib.sha256(raw).hexdigest()
            if actual!=args.expected_dataset_sha:
                raise ValueError('Dataset SHA does not match')
            rules=args.rules_file.read_text(encoding='utf-8') if args.rules_file else '{}'
            data=json.loads(raw)
            report=evaluate_dataset(data,dataset_sha256=actual,rules=rules,
                receipt_check=lambda row:screenshot_check(args.input.parent,row))
            private_json(args.output,report)
        print('Private audit artifact saved. No settings were enabled.')
        return 0
    except (ValueError,TypeError,OSError) as exc:
        print(f'Audit refused ({type(exc).__name__}); no private source contents printed',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
