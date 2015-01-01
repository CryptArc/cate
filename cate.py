import praw
from bitcoin.base58 import decode
from decimal import Decimal
import hashlib
import os.path
from StringIO import StringIO
import yaml
import sys

from bitcoin.core import *
from bitcoin.core.script import *

ADDRESS_LENGTH = 25

# hashes of the genesis blocks for each network
NETWORK_HASHES = {
  'BTC': '000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943',
  'DOGE': 'bb0a78264637406b6360aad926284d544d7049f45189db5664f3c4d07350559e',
  'LTC': 'f5ae71e26c74beacc88382716aced69cddf3dffff24f384e1808905e0188f68f'
}

# Reverse-map of NETWORK_HASHES
NETWORK_CODES = {
  '000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943': 'BTC',
  'bb0a78264637406b6360aad926284d544d7049f45189db5664f3c4d07350559e': 'DOGE',
  'f5ae71e26c74beacc88382716aced69cddf3dffff24f384e1808905e0188f68f': 'LTC'
}

# Generates "TX1" from the guide at https://en.bitcoin.it/wiki/Atomic_cross-chain_trading
# Pay w BTC to <B's public key> if (x for H(x) known and signed by B) or (signed by A & B)
#
# proxy is the RPC proxy to the relevant daemon JSON-RPC interface
# quantity is the number of coins to be sent, expressed as a decimal
# peer_public_key is the public key with which the payment and refund transactions must be signed, expressed as CBase58Data
# own_public_key is the public key this client signs the refund transaction with, expressed as CBase58Data
# secret the secret value to be revealed for the payment transaction
#
# returns a CTransaction
def build_tx1(proxy, quantity, own_address, peer_address, secret):
  # TODO: Calculate fee usefully
  fee = Decimal(1.00000000)
  total_quantity_needed = quantity + fee

  total_in = Decimal(0.00000000)
  txins = []
  for txout in proxy.listunspent(0):
    total_in += (txout['amount'] / COIN)
    txins.append(CMutableTxIn(txout['outpoint']))
    # TODO: Lock outputs?
    if total_in >= total_quantity_needed:
      break

  if total_in < total_quantity_needed:
    raise Exception('Insufficient funds.')

  # scriptSig is either:
  #     0 <signature B> <signature A> 2 <A public key> <B public key> 2
  # or
  #     <shared secret> <signature B> <B public key> 0
  tx_script = CScript(
    [
      OP_IF,
        OP_2DUP, # Multisig
          OP_HASH160, peer_address, OP_EQUALVERIFY,
          OP_HASH160, own_address, OP_EQUALVERIFY,
          2, OP_CHECKMULTISIG,
      OP_ELSE,
        OP_DUP, # Single sig + hash
          OP_HASH160, peer_address, OP_EQUALVERIFY,
          OP_CHECKSIGVERIFY,
          OP_HASH160, Hash160(secret), OP_EQUAL,
      OP_ENDIF
    ]
  )
  txout = CTxOut(quantity * COIN, tx_script)
  txouts = [txout]

  # Generate a change transaction if needed
  if total_in > total_quantity_needed:
    change = total_in - total_quantity_needed
    change_address = proxy.getrawchangeaddress()
    change_txout = CTxOut(change * COIN, change_address.to_scriptPubKey())
    txouts.append(change_txout)

  tx = CMutableTransaction(txins, txouts)
  tx_signed = proxy.signrawtransaction(tx)
  if not tx_signed['complete']:
      raise Exception('Transaction came back without all inputs signed.')

  return tx_signed['tx']

# Generates "TX2" from the guide at https://en.bitcoin.it/wiki/Atomic_cross-chain_trading
# Pay w BTC from TX1 to <A's public key>, locked 48 hours in the future, signed by A
#
# proxy is the RPC proxy to the relevant daemon JSON-RPC interface
# trade_id the ID of the trade, for use when generating the refund address
# tx1 the (complete, signed) transaction to refund from
# quantity the number of coins being refunded
# own_address is the private address this client signs the refund transaction with. This must match
#     the address provided when generating TX1.
#
# returns a CTransaction
def build_tx2(proxy, tx1, quantity, own_address):
  prev_txid = tx1.GetHash()
  prev_out = tx1.vout[0]
  txin = CMutableTxIn(COutPoint(prev_txid, 0))
  txins = [txin]

  # TODO: Sign the transaction with our key
  seckey = proxy.dumpprivkey(own_address)

  txin_scriptPubKey = prev_out.scriptPubKey

  # TODO: Allow fees out of the amount refunded
  txouts = [CTxOut(quantity * COIN, own_address.to_scriptPubKey())]

  # Create the unsigned transaction
  tx = CMutableTransaction(txins, txouts)

  # Calculate the signature hash for that transaction.
  sighash = SignatureHash(txin_scriptPubKey, tx, 0, SIGHASH_ALL)

  # Now sign it. We have to append the type of signature we want to the end, in
  # this case the usual SIGHASH_ALL.
  sig = seckey.sign(sighash) + bytes([SIGHASH_ALL])

  # Set the scriptSig of our transaction input appropriately.
  txin.scriptSig = CScript([sig, seckey.pub])

  # Mutate the TX into something we can serialize?

  return tx

"""
Generates a directory path within which to store audit details for a trade.
Returns the path to the directory.
"""
def ensure_audit_directory_exists(trade_id):
  if not os.path.isdir('audits'):
    os.mkdir('audits')

  # Log the incoming offer
  audit_directory = 'audits' + os.path.sep + trade_id
  if not os.path.isdir(audit_directory):
    os.mkdir(audit_directory)

  return audit_directory

def load_configuration(filename):
  if not os.path.isfile(filename):
    raise Exception("Expected configuration file '" + filename + "'")

  with open(filename, 'r') as f:
    raw_config = f.read(10 * 1024 * 1024) # If you have a config over 10M in size, I give up

  try:
    config = yaml.load(raw_config)
  except yaml.parser.ParserError as e:
    raise Exception("Could not parse configuration file: {0}".format(e))

  return config

def reddit_login(r, config):
  """
  Read the configuration from disk and log in to reddit. Throws Exception
  in case of problems
  """

  # Check the configuration has a reddit section before trying to log in
  if 'reddit' not in config:
    raise Exception("Expected 'reddit' section in configuration file 'config.yml'")

  # Try to log in
  reddit_config = config['reddit']
  if 'username' not in reddit_config or 'password' not in reddit_config:
    raise Exception("Expected reddit username and password to be provided in password file")

  try:
    r.login(reddit_config['username'].strip(), reddit_config['password'].strip())
  except praw.errors.InvalidUserPass:
    raise Exception("Could not log in to reddit with provided username and password")

  return

def validate_address(address):
  """
  Validates a Bitcoin-format pay-to-key address. Takes in the address as a
  string and returns True if the address is valid, False otherwise.
  """
  if len(address) < ADDRESS_LENGTH:
    return False
  else:
    addr = decode(address)
    if not addr:
      return False
    else:
      version = addr[0]
      checksum = addr[-4:] # Get the last 4 characters of the address
      vh160 = addr[:-4] # Version plus hash160 is what is checksummed

      digester = hashlib.sha256()
      digester.update(vh160)
      vh160_hash = digester.digest()

      digester = hashlib.sha256()
      digester.update(vh160_hash)
      vh160_hash_hash = digester.digest()

      if vh160_hash_hash[0:4] != checksum:
        return False
  return True