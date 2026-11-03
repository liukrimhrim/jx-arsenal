
# awsx lever

One stdlib Python CLI over `aws`. TSV by default, `--json` for the raw payload. `-p PROFILE`
(or `AWS_PROFILE`), `--region`. Every call runs with `TZ=UTC`, so timestamps never arrive in local time.

## Start here

```bash
jx aws -p prod whoami     # account, role ARN, and SSO token validity ("valid 23m" / EXPIRED, exit 1)
```

`sts get-caller-identity` keeps succeeding from cached role credentials for hours after the SSO
token expired, while terraform and SDKs fail. `whoami` reads the token's `expiresAt` from
`~/.aws/sso/cache` and tells you to `aws sso login` when it is gone.

## Metrics, alarms, images, cost, logs, EKS

```bash
jx aws -p prod cw AWS/RDS                                   # list metrics + dimensions in a namespace
jx aws -p prod cw AWS/RDS CPUUtilization --dim DBInstanceIdentifier=db-1 --since 6h --period 300
jx aws -p prod cw AWS/SQS ApproximateNumberOfMessagesVisible --dim QueueName=q --stat Maximum,SampleCount
jx aws -p prod alarms --state ALARM [--prefix name]
jx aws -p prod ecr-tags my-repo --limit 10 [--filter substr]   # newest first; tags, digest, size
jx aws -p prod cost --since 30d --group SERVICE --top 10        # also LINKED_ACCOUNT, USAGE_TYPE, REGION, TAG:key
jx aws -p prod cost --since 7d --granularity DAILY               # totals per day
jx aws -p prod logs-query '/aws/lambda/fn' 'fields @timestamp, @message | filter @message like /ERROR/ | sort @timestamp desc' --since 2h
jx aws -p prod eks my-cluster            # version, platform, endpoint access, auth mode
jx aws -p prod eks my-cluster addons     # addon versions, status, health issues
jx aws -p prod eks my-cluster nodegroups
```

## Reading CloudWatch numbers

Default stats are `Average,SampleCount`. A gauge summed over a period is value × samples, not a
count of anything; `--stat Sum` is for true counters and the tool warns when Sum is requested
without SampleCount. `cost` end date is exclusive and includes today's partial day.
